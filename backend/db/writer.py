"""
backend/db/writer.py -- event-bus subscriber that persists frames.

Subscribes to the event bus independently of ws.manager. Never imports pyserial.
Started/stopped as an asyncio task by main.py's lifespan (after init_db()).

Per event (see serial_reader.SerialReader._handle_frame for the shape):
  1. INSERT into raw_frames                    -- ALWAYS (forensic log, CRC pass or fail)
  2. UPSERT nodes.last_seen = received_at      -- for every frame that carries a node_id
  3. INSERT into readings                      -- ONLY when crc_ok, and
     ON CONFLICT (node_id, seq_num) DO NOTHING -- so replaying a capture / restarting
                                                  the reader is idempotent

One transaction per event (the node rate is ~2 Hz -- batching is a later concern).
A failure on one event is logged and skipped; it never kills the loop.

TODO:
  - [ ] Batch commits (N rows / 100 ms) if ingestion volume ever grows.
  - [ ] Prune/rollup raw_frames.
"""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from .models import Node, RawFrame, Reading
from .session import SessionLocal

log = logging.getLogger(__name__)

_NONE3 = (None, None, None)


class DBWriter:
    def __init__(self, bus, session_factory=None):
        self._bus = bus
        self._session_factory = session_factory or SessionLocal
        self._task: asyncio.Task | None = None
        self._raw_written = 0
        self._readings_written = 0
        self._readings_duplicate = 0
        self._errors = 0

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
            "errors": self._errors,
        }

    # -- loop ---------------------------------------------------------- #
    async def _run(self) -> None:
        log.info("db writer subscribed to event bus")
        async for event in self._bus.subscribe():
            try:
                await self._persist(event)
            except Exception:
                self._errors += 1
                log.exception(
                    "db writer: failed to persist frame (node=%s seq=%s)",
                    event.get("node_id"), event.get("seq_num"),
                )

    async def _persist(self, event: dict) -> None:
        node_id = event["node_id"]
        received_at = event["received_at"]

        async with self._session_factory() as session:
            async with session.begin():
                # 1. forensic log -- always
                session.add(RawFrame(
                    node_id=node_id,
                    received_at=received_at,
                    crc_ok=event["crc_ok"],
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

                # 3. decoded reading -- CRC-valid only, idempotent on (node_id, seq_num)
                inserted_reading = False
                if event["crc_ok"] and node_id is not None:
                    values = event["values"]
                    ax, ay, az = values.get("accel_mss") or _NONE3
                    gx, gy, gz = values.get("gyro_rads") or _NONE3
                    result = await session.execute(
                        sqlite_insert(Reading)
                        .values(
                            node_id=node_id,
                            seq_num=event["seq_num"],
                            timestamp=received_at,
                            tof_mm=values.get("tof_mm"),
                            accel_x=ax, accel_y=ay, accel_z=az,
                            gyro_x=gx, gyro_y=gy, gyro_z=gz,
                        )
                        .on_conflict_do_nothing(index_elements=["node_id", "seq_num"])
                    )
                    inserted_reading = bool(result.rowcount)

        # counters updated after the transaction commits cleanly
        self._raw_written += 1
        if event["crc_ok"] and node_id is not None:
            if inserted_reading:
                self._readings_written += 1
            else:
                self._readings_duplicate += 1
