"""
backend/db/writer.py -- event-bus subscriber that persists frames.

Subscribes to the event bus independently of ws.manager. Never imports pyserial.
Started/stopped as an asyncio task by main.py's lifespan (after init_db()).

Per event (see serial_reader.SerialReader._handle_frame for the shape):
  1. INSERT into raw_frames                    -- ONLY when NOT crc_ok. CRC-valid
                                                  frames are no longer persisted:
                                                  they live for ~40 min in the
                                                  in-memory ring buffer
                                                  (ingest/frame_buffer.py) and
                                                  nowhere on disk. CRC failures
                                                  are rare and kept indefinitely.
  2. UPSERT nodes.last_seen = received_at      -- for every frame that carries a node_id
  3. INSERT into readings                      -- ONLY when crc_ok, and
     ON CONFLICT (node_id, seq_num) DO NOTHING -- so replaying a capture / restarting
                                                  the reader is idempotent WITHIN a
                                                  session

Node restart handling: seq_num is per-node and resets to ~0 when a node reboots,
so the fresh session would collide with the previous session's rows and be
silently dropped by the ON CONFLICT guard (readings would freeze; see
protocol-spec.md section 6). When a clear backward jump in seq_num after a gap of
silence marks a restart, this writer drops that node's prior `readings` so the
new session ingests. `raw_frames` (the forensic log) is never touched.

One transaction per event (the node rate is ~2 Hz -- batching is a later concern).
A failure on one event is logged and skipped; it never kills the loop.

`readings` retention/rollup lives in db/retention.py; `raw_frames` needs none
now that only CRC failures land there.

TODO:
  - [ ] Batch commits (N rows / 100 ms) if ingestion volume ever grows.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from .models import Node, RawFrame, Reading
from .session import SessionLocal

log = logging.getLogger(__name__)

_NONE3 = (None, None, None)

# -- node-restart (seq reset) detection -------------------------------------- #
SEQ_SPACE = 0x10000            # seq_num is uint16
# A backward move in seq_num of at least this much is not reorder jitter...
RESTART_BACKSTEP_MIN = 256
# ...and one within this much of a full wrap IS the legitimate 0xFFFF->0x0000
# rollover, not a restart (a restart lands near 0 from an arbitrary seq_num,
# whereas a wrap starts from near the top of the space).
RESTART_WRAP_BAND = SEQ_SPACE - RESTART_BACKSTEP_MIN
# ...and the node must have gone quiet first (a reboot, not a reordered burst).
RESTART_SILENCE_S = 2.0


def _backstep(prev_seq: int, seq: int) -> int:
    """How far ``seq`` moved backward from ``prev_seq``. Negative = forward."""
    return prev_seq - seq


def _looks_like_restart(
    prev_seq: int | None, prev_ts: datetime | None, seq: int, now: datetime
) -> bool:
    """A node reboot: ``seq_num`` resets near 0 from an arbitrary point, after
    the node has gone quiet. Deliberately excludes the legitimate end-of-space
    wrap (``_looks_like_wrap``, below) -- that is a routine event mid-session,
    not a restart, and must not be counted or logged as one.
    """
    if prev_seq is None:
        return False
    backstep = _backstep(prev_seq, seq)
    if backstep < RESTART_BACKSTEP_MIN or backstep > RESTART_WRAP_BAND:
        return False
    if prev_ts is not None and (now - prev_ts).total_seconds() < RESTART_SILENCE_S:
        return False
    return True


def _looks_like_wrap(prev_seq: int | None, seq: int) -> bool:
    """The legitimate ``0xFFFF -> 0x0000`` rollover, mid-session.

    ``seq_num`` is uint16, so at ~2 Hz it wraps roughly every 9.1 h of
    continuous uptime -- inside ``READINGS_FULL_RES_HOURS``' default 168 h
    window. Without clearing on this event, the very first reading after a
    wrap collides with the still-resident row from ~9 h earlier on the
    ``(node_id, seq_num)`` unique constraint; ``ON CONFLICT DO NOTHING``
    silently discards it, and then EVERY reading after that too, forever --
    nothing ever frees that seq_num again within the window. `readings`
    ingestion for that node would freeze permanently, invisibly (only
    ``readings_duplicate`` climbing, uncapped and unalarmed, in `/health`).

    The live `/live` feed and the archive tier are unaffected either way --
    neither dedupes on `seq_num` -- so nothing is actually lost; only the
    SQLite-backed History view would silently stop advancing. This is a
    structural ceiling on `readings`, not a bug to route around: a single
    node's full-resolution capacity there is bounded by `seq_num`'s ~9.1 h
    range regardless of `READINGS_FULL_RES_HOURS`. The archive is where full
    resolution actually lives past that.

    No silence check, unlike a restart: a wrap happens mid-stream during
    perfectly healthy, continuous operation, not after a gap.
    """
    if prev_seq is None:
        return False
    return _backstep(prev_seq, seq) > RESTART_WRAP_BAND


class DBWriter:
    def __init__(self, bus, session_factory=None):
        self._bus = bus
        self._session_factory = session_factory or SessionLocal
        self._task: asyncio.Task | None = None
        self._raw_written = 0
        self._readings_written = 0
        self._readings_duplicate = 0
        self._sessions_reset = 0
        self._wraps_handled = 0
        self._errors = 0
        # per-node: last seq_num and receipt time of a reading-bearing frame
        self._last_seq: dict[int, int] = {}
        self._last_seen_ts: dict[int, datetime] = {}

    # -- lifecycle ------------------------------------------------------- #
    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="db-writer")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    def status(self) -> dict:
        return {
            "running": self._task is not None and not self._task.done(),
            "raw_written": self._raw_written,
            "readings_written": self._readings_written,
            "readings_duplicate": self._readings_duplicate,
            "sessions_reset": self._sessions_reset,
            "wraps_handled": self._wraps_handled,
            "errors": self._errors,
        }

    # -- loop ---------------------------------------------------------- #
    async def _run(self) -> None:
        log.info("db writer subscribed to event bus")
        async for event in self._bus.subscribe("db-writer"):
            try:
                await self._persist(event)
            except Exception:
                self._errors += 1
                log.exception(
                    "db writer: failed to persist frame (node=%s seq=%s)",
                    event.get("node_id"), event.get("seq_num"),
                )

    async def _persist(self, event: dict) -> None:
        # Control frames (shutdown ACK etc.) are not telemetry: no reading, no
        # last_seen bump, nothing. FIRST statement, before any DB work -- an ACK's
        # values dict is non-empty, so without this it would be stored as a reading.
        if event.get("control"):
            return
        node_id = event["node_id"]
        received_at = event["received_at"]
        seq = event["seq_num"]

        crc_ok = event["crc_ok"]

        async with self._session_factory() as session:
            async with session.begin():
                # 1. forensic log -- CRC failures only. CRC-valid frames are kept
                #    transiently in the in-memory ring buffer instead (see
                #    ingest/frame_buffer.py), never written to disk.
                if not crc_ok:
                    session.add(RawFrame(
                        node_id=node_id,
                        received_at=received_at,
                        crc_ok=False,
                        raw=event["raw"],
                    ))

                if node_id is not None:
                    # 2. node liveness -- upsert last_seen on every framed packet
                    await session.execute(
                        sqlite_insert(Node)
                        .values(node_id=node_id, last_seen=received_at)
                        .on_conflict_do_update(
                            index_elements=["node_id"],
                            set_={"last_seen": received_at},
                        )
                    )

                # 3. decoded reading -- CRC-valid frames that actually carried
                #    decoded values; idempotent on (node_id, seq_num). Empty-payload
                #    keepalive frames update last_seen (above) but are not readings.
                values = event["values"]
                store_reading = crc_ok and node_id is not None and bool(values)
                inserted_reading = False
                if store_reading:
                    prev_seq = self._last_seq.get(node_id)
                    prev_ts = self._last_seen_ts.get(node_id)
                    if prev_seq is None:
                        # first reading-bearing frame for this node since the
                        # writer started -- seed from disk so a restart that
                        # happened while the backend was down is still caught.
                        seeded = (await session.execute(
                            select(func.max(Reading.seq_num), func.max(Reading.timestamp))
                            .where(Reading.node_id == node_id)
                        )).one()
                        prev_seq, prev_ts = seeded[0], seeded[1]

                    if _looks_like_restart(prev_seq, prev_ts, seq, received_at):
                        await session.execute(
                            delete(Reading).where(Reading.node_id == node_id)
                        )
                        self._sessions_reset += 1
                        log.warning(
                            "node %s seq restarted (last %s -> now %s); cleared its "
                            "prior readings so the new session ingests -- raw_frames "
                            "is untouched",
                            node_id, prev_seq, seq,
                        )
                    elif _looks_like_wrap(prev_seq, seq):
                        # Routine, not a fault -- see _looks_like_wrap's docstring
                        # for why this must clear too, not just skip. INFO, not
                        # WARNING: this is expected to happen roughly every 9 h of
                        # continuous uptime, unlike an actual restart.
                        await session.execute(
                            delete(Reading).where(Reading.node_id == node_id)
                        )
                        self._wraps_handled += 1
                        log.info(
                            "node %s seq wrapped (last %s -> now %s, routine "
                            "0xFFFF->0x0000 rollover, not a restart); cleared its "
                            "prior readings so the new lap can ingest -- archive/ "
                            "and /live are unaffected, nothing is actually lost",
                            node_id, prev_seq, seq,
                        )

                    ax, ay, az = values.get("accel_mss") or _NONE3
                    gx, gy, gz = values.get("gyro_rads") or _NONE3
                    result = await session.execute(
                        sqlite_insert(Reading)
                        .values(
                            node_id=node_id,
                            seq_num=seq,
                            timestamp=received_at,
                            tof_mm=values.get("tof_mm"),
                            tof_out_of_range=values.get("tof_out_of_range"),
                            accel_x=ax, accel_y=ay, accel_z=az,
                            gyro_x=gx, gyro_y=gy, gyro_z=gz,
                        )
                        .on_conflict_do_nothing(index_elements=["node_id", "seq_num"])
                    )
                    inserted_reading = bool(result.rowcount)

        # state + counters updated after the transaction commits cleanly
        if not crc_ok:
            self._raw_written += 1  # raw_frames now holds CRC failures only
        if store_reading:
            self._last_seq[node_id] = seq
            self._last_seen_ts[node_id] = received_at
            if inserted_reading:
                self._readings_written += 1
            else:
                self._readings_duplicate += 1
