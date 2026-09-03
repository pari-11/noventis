"""
backend/archive/writer.py -- append-only archive of every CRC-valid reading.

The fourth independent event-bus subscriber, alongside frame_buffer, db.writer
and ws.manager (constraint #4). Never imports pyserial.

WHY THIS EXISTS
    SQLite is currently both the system of record and the query engine for the
    dashboard. Those jobs conflict: keep data and the file grows, roll it up and
    the fine detail is destroyed. The 2026-09-01 capture was lost exactly that
    way -- retention averaged it into 1-minute buckets on a timer and deleted the
    originals, because there was no other copy.

    This module splits the roles. Every CRC-valid reading is appended here at full
    resolution, forever. SQLite becomes a disposable cache in front of it.

TWO THREADS, ON PURPOSE
    ``open()``, ``write()`` and especially ``fsync()`` are blocking syscalls, and
    fsync has unbounded worst-case latency -- a busy disk can hold it for hundreds
    of milliseconds. Inline in the event loop that would freeze the WebSocket
    broadcast and every HTTP handler (constraint #3, which exists for exactly this
    reason on the serial side).

    So the bus subscriber does **one** ``put_nowait`` into a ``queue.Queue`` and
    nothing else -- no serialising, no file handles, no syscalls -- making this
    the cheapest subscriber on the bus, which is what the durability accounting
    below requires of it. A dedicated thread owns every file operation.

    Not ``aiofiles`` and not ``to_thread`` per write: both leave the loop awaiting
    a syscall with unbounded latency, and the shared executor means a stuck fsync
    can starve other ``to_thread`` callers -- including ``reader.stop``.

TEXT FIRST, COMPRESS LATER
    Live writes go to newline-delimited JSON. A crash mid-write costs at most one
    truncated line; a columnar or compressed file would be unreadable in its
    entirety, because it buffers a block in memory and needs a valid footer. So
    the crash-exposed path is the dumb one, and compaction happens only after an
    hour is closed and can safely be retried.

HONEST DURABILITY
    The bus drops events when a subscriber lags -- by design, and correctly, since
    blocking ingestion would be worse. But this tier claims not to lose readings,
    so a drop it did not notice would make it *lie*: compaction would stamp the
    hour complete, retention would trust that and delete the SQLite copy, and the
    readings would be gone while everything reported success. That is worse than
    today's honest timer-based deletion.

    Therefore: every drop -- from the bus (detected via ``bus.dropped_for``),
    from this module's own queue, or from an unhandled exception while
    processing one event (``_run``'s per-event try/except, mirroring
    ``db.writer``'s) -- writes a ``_gap`` record **into the spool file itself**,
    so the taint travels with the data and survives a crash that would erase an
    in-memory counter. Any hour containing a gap is ``complete: false`` and can
    never be certified. The per-event guard also means one malformed event can
    never silently kill the subscriber for the rest of the process's life --
    found live 2026-09-05: the loop here had no such guard, unlike ``db.writer``,
    so a single bad event would have stopped archiving permanently while the
    dashboard kept looking completely healthy.

    ``complete`` means **no loss observed by the backend** -- nothing stronger is
    knowable here. A frame lost over the air, or one the node never sent, leaves
    no trace but a seq_num gap, and no backend design recovers it (protocol-spec
    section 6). ``seq_gaps_observed`` is recorded separately and deliberately does
    NOT clear ``complete``, because a gap there is usually RF loss, not our fault.

LAYOUT
    archive/spool/node=1/2026-09-03T05.ndjson        <- being appended now
    archive/data/readings/node_id=1/date=2026-09-03/hour=05/part-0000.ndjson.gz
    archive/data/readings/node_id=1/date=2026-09-03/hour=05/_manifest.json

    Manifests are sidecar files next to the data, never rows in SQLite. State
    about the archive lives with the archive -- put it in the database and
    deleting the database would destroy the record of what was safely stored,
    reintroducing the coupling this design exists to remove.

FIXED ROW SHAPE
    Every row carries all seven value columns, IMU included, as explicit nulls
    until the sensor is fitted. A sparse shape would mean the file format changes
    the day the IMU lands, breaking reconciliation and any later columnar
    conversion.

NOT YET
    Nothing reads this back (a DuckDB tier over GET /readings is the deferred
    step), and retention does NOT consult the manifests yet -- deletion stays on
    its existing timer until the archive has been reconciled against `readings`
    for a while. This tier is write-only insurance for now, by design.
"""

from __future__ import annotations

import asyncio
import collections
import gzip
import json
import logging
import os
import queue
import shutil
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .store import ArchiveStore, fsync_dir, sha256_file, store_from_env

log = logging.getLogger(__name__)

# -- thresholds ------------------------------------------------------------- #
ARCHIVE_DIR = Path(os.getenv("NOVENTIS_ARCHIVE_DIR", "./archive"))
# Hand-off queue depth. 20k rows is ~2.8 h at 2 Hz for one node: overflow at that
# depth is not a hiccup, it means the disk has been unwritable for hours, and
# tainting the hour is then the right answer.
ARCHIVE_QUEUE_MAX = int(os.getenv("NOVENTIS_ARCHIVE_QUEUE_MAX", "20000"))
# Rows per write() call. Bounds how long the writer thread holds the GIL while
# serialising (~1-2 ms at 500), which is the only cost this design imposes on the
# event loop -- write() and fsync() themselves release it.
ARCHIVE_BATCH_MAX = int(os.getenv("NOVENTIS_ARCHIVE_BATCH_MAX", "500"))
# How often the spool is forced to disk. THIS IS THE DATA-LOSS WINDOW on a hard
# power cut: everything written but not yet fsynced dies. Lowering it costs more
# fsyncs; raising it widens the exposure. It cannot be made zero without an fsync
# per reading, which is the stall this module exists to avoid.
ARCHIVE_FSYNC_INTERVAL_S = float(os.getenv("NOVENTIS_ARCHIVE_FSYNC_INTERVAL_S", "5"))
# How long past the end of an hour to wait before closing and compacting it, so a
# late-arriving frame is not stranded after its file was sealed.
ARCHIVE_HOUR_GRACE_S = float(os.getenv("NOVENTIS_ARCHIVE_HOUR_GRACE_S", "60"))
# Idle wake-up, so a silent node still gets its finished hours closed and
# compacted instead of waiting for the next frame to arrive.
ARCHIVE_IDLE_POLL_S = float(os.getenv("NOVENTIS_ARCHIVE_IDLE_POLL_S", "1"))

SEQ_SPACE = 0x10000  # seq_num is uint16

_SENTINEL = object()

# Order matters: this is the column order of every archived row.
VALUE_COLUMNS = (
    "tof_mm", "tof_out_of_range",
    "accel_x", "accel_y", "accel_z",
    "gyro_x", "gyro_y", "gyro_z",
)


def archive_row(event: dict) -> dict:
    """Bus event -> one archive row. Fixed shape; missing sensors become null.

    Deliberately drops ``raw``/``raw_hex``: the ring buffer already holds those,
    and they roughly double the memory of a queued row for data the archive does
    not need.
    """
    values = event.get("values") or {}
    accel = values.get("accel_mss") or (None, None, None)
    gyro = values.get("gyro_rads") or (None, None, None)
    received = event["received_at"]
    return {
        "node_id": event.get("node_id"),
        "seq_num": event.get("seq_num"),
        "ts": received.isoformat(),
        "tof_mm": values.get("tof_mm"),
        "tof_out_of_range": values.get("tof_out_of_range"),
        "accel_x": accel[0], "accel_y": accel[1], "accel_z": accel[2],
        "gyro_x": gyro[0], "gyro_y": gyro[1], "gyro_z": gyro[2],
    }


def _hour_key(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H")


def _hour_start(hour_key: str) -> datetime:
    return datetime.strptime(hour_key, "%Y-%m-%dT%H").replace(tzinfo=timezone.utc)


def _partition_key(node_id: int, hour_key: str, name: str) -> str:
    date, hour = hour_key.split("T")
    return f"readings/node_id={node_id}/date={date}/hour={hour}/{name}"


class _Spool:
    """One open append-only file: (node, hour). All access is from the writer thread."""

    def __init__(self, path: Path, node_id: int, hour_key: str) -> None:
        self.path = path
        self.node_id = node_id
        self.hour_key = hour_key
        self.rows = 0
        self.gaps = 0
        self.seq_gaps = 0
        path.parent.mkdir(parents=True, exist_ok=True)
        self.fh = open(path, "a", encoding="utf-8", newline="\n")

    def write_rows(self, rows: list[dict]) -> None:
        self.fh.write("".join(json.dumps(r, separators=(",", ":")) + "\n" for r in rows))
        self.rows += len(rows)

    def write_gap(self, dropped: int, reason: str) -> None:
        """Record a hole IN THE FILE, so the taint survives a crash."""
        self.fh.write(json.dumps({
            "_gap": {
                "at": datetime.now(timezone.utc).isoformat(),
                "dropped": dropped,
                "reason": reason,
            }
        }, separators=(",", ":")) + "\n")
        self.gaps += 1
        self.flush(force=True)
        log.warning(
            "archive: %d event(s) lost (%s) -- hour %s for node %s is now incomplete",
            dropped, reason, self.hour_key, self.node_id,
        )

    def flush(self, force: bool = False) -> None:
        self.fh.flush()
        if force:
            os.fsync(self.fh.fileno())

    def close_clean(self) -> None:
        """Seal the file with a close record. Its absence marks an unclean stop."""
        self.fh.write(json.dumps({
            "_close": {
                "at": datetime.now(timezone.utc).isoformat(),
                "rows": self.rows,
                "gaps": self.gaps,
                "seq_gaps": self.seq_gaps,
            }
        }, separators=(",", ":")) + "\n")
        self.flush(force=True)
        self.fh.close()


def scan_spool_file(path: Path) -> dict:
    """Read a finished spool file back: row count, gaps, and whether it was sealed.

    A file with no ``_close`` record was left open by a process that died, so
    anything after its last fsync is missing -- it can never be certified.
    """
    rows = gaps = seq_gaps = 0
    closed = False
    truncated = False
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                # Only ever the final line, and only after a crash mid-write --
                # which is precisely why this format is line-oriented.
                truncated = True
                continue
            if "_gap" in obj:
                gaps += 1
            elif "_close" in obj:
                closed = True
                seq_gaps = obj["_close"].get("seq_gaps", 0)
            else:
                rows += 1
    return {"rows": rows, "gaps": gaps, "seq_gaps": seq_gaps,
            "closed": closed, "truncated": truncated}


class ArchiveWriter:
    """Bus subscriber (async, trivial) + writer thread (blocking, owns all I/O)."""

    def __init__(self, bus, archive_dir: Path | str = ARCHIVE_DIR,
                 store: ArchiveStore | None = None) -> None:
        self._bus = bus
        self._dir = Path(archive_dir)
        self._spool_dir = self._dir / "spool"
        self._store = store if store is not None else store_from_env(self._dir)

        self._q: queue.Queue = queue.Queue(maxsize=ARCHIVE_QUEUE_MAX)
        self._task: asyncio.Task | None = None
        self._thread: threading.Thread | None = None

        # counters (read by /health; ints, so reads are atomic enough)
        self._rows_queued = 0
        self._rows_written = 0
        self._queue_drops = 0
        self._bus_drops_seen = 0
        self._segments = 0
        self._errors = 0
        self._incomplete_hours = 0
        self._last_fsync_at: float | None = None
        self._last_row_at: str | None = None

        self._spools: dict[tuple[int, str], _Spool] = {}
        self._last_seq: dict[int, int] = {}
        # Out-of-band hole notices, async side -> writer thread. Never in _q.
        self._gap_notices: collections.deque = collections.deque()
        # Holes that arrived with no spool open: applied to the next one created,
        # so a drop during a silent period still taints the hour it lands in.
        self._orphan_gaps: list[tuple[int, str]] = []

    # -- lifecycle ------------------------------------------------------- #
    def start(self) -> None:
        self._spool_dir.mkdir(parents=True, exist_ok=True)
        self._recover_orphans()
        self._recover_last_seq()
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(
                target=self._thread_main, name="archive-writer", daemon=True
            )
            self._thread.start()
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="archive-subscriber")

    async def stop(self, drain_timeout: float = 5.0) -> None:
        """Drain, then stop. Cancelling outright would discard queued readings.

        Order matters: the caller stops the serial reader first, so no new frames
        arrive. Then the bus queue is drained into the writer thread, then the
        sentinel tells the thread to seal its files. Cancelling the subscriber
        first would throw away whatever the bus still held -- losing the last
        seconds of every run, in the tier whose whole purpose is losing nothing.
        """
        if self._task is not None:
            deadline = time.monotonic() + drain_timeout
            while time.monotonic() < deadline:
                if self._bus_pending() == 0:
                    break
                await asyncio.sleep(0.05)
            else:
                log.warning("archive: bus queue still had events at shutdown deadline")
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

        if self._thread is not None and self._thread.is_alive():
            self._q.put(_SENTINEL)
            await asyncio.to_thread(self._thread.join, 30.0)
            if self._thread.is_alive():  # pragma: no cover
                log.error("archive writer thread did not stop within 30s")
        self._thread = None

    def _bus_pending(self) -> int:
        for q, sub in getattr(self._bus, "_subscribers", {}).items():
            if sub.name == "archive":
                return q.qsize()
        return 0

    # -- async side: one put_nowait, nothing else ------------------------ #
    async def _run(self) -> None:
        log.info("archive writer subscribed to event bus (dir=%s, store=%r)",
                 self._dir, self._store)
        async for event in self._bus.subscribe("archive"):
            if not event.get("crc_ok"):
                continue
            try:
                # Did the BUS drop anything for us since last time? If so the
                # hole is already in the past; record it before the next row so
                # the marker lands in the right hour.
                dropped = self._bus.dropped_for("archive")
                if dropped > self._bus_drops_seen:
                    missed = dropped - self._bus_drops_seen
                    self._bus_drops_seen = dropped
                    self._note_gap(missed, "bus")
                self._offer(archive_row(event))
            except Exception:
                # Mirrors db.writer's per-event guard: one bad event must not
                # take the whole subscriber down. Without this, an unhandled
                # exception here (e.g. archive_row() on a malformed event) kills
                # this async-for permanently -- `status()["subscribed"]` flips to
                # False, but nothing polls or alerts on that, so the archive
                # would silently stop recording for the rest of the process's
                # life while the live feed and SQLite kept working normally,
                # looking completely healthy from the dashboard.
                #
                # Counted as a drop, not just an error: this reading genuinely
                # never reached the archive, so the currently open hour(s) must
                # not be certifiable as complete -- the same honesty this module
                # already applies to a bus or internal-queue drop.
                self._errors += 1
                self._note_gap(1, "processing_error")
                log.exception(
                    "archive: could not process event (node=%s seq=%s) -- "
                    "counted as a drop so the hour cannot be certified complete "
                    "on data actually lost here",
                    event.get("node_id"), event.get("seq_num"),
                )

    def _note_gap(self, n: int, reason: str) -> None:
        """Signal a hole to the writer thread OUT OF BAND.

        Never through ``self._q``. The queue being full is itself a drop cause, so
        a marker enqueued there is discarded exactly when it is needed -- the
        writer would then seal the hour ``complete: true`` having silently lost
        readings, which is the precise failure this tier exists to prevent (and
        is worse than the honest timer-based deletion it replaces). ``deque``
        append/popleft are atomic under the GIL, so this needs no lock.
        """
        self._gap_notices.append((n, reason))

    def _offer(self, item) -> None:
        try:
            self._q.put_nowait(item)
            self._rows_queued += 1
        except queue.Full:
            self._queue_drops += 1
            self._note_gap(1, "queue")

    # -- thread side: every file operation lives here -------------------- #
    def _thread_main(self) -> None:
        log.info("archive writer thread started")
        pending: list[dict] = []
        last_fsync = time.monotonic()
        try:
            while True:
                stop = False
                try:
                    item = self._q.get(timeout=ARCHIVE_IDLE_POLL_S)
                except queue.Empty:
                    item = None
                if item is _SENTINEL:
                    stop = True
                elif item is not None:
                    pending.append(item)
                    # Drain whatever else is ready into one write().
                    while len(pending) < ARCHIVE_BATCH_MAX:
                        try:
                            nxt = self._q.get_nowait()
                        except queue.Empty:
                            break
                        if nxt is _SENTINEL:
                            stop = True
                            break
                        pending.append(nxt)

                # Holes are recorded from their own channel, so a full row queue
                # can never swallow the notice that the row queue is full.
                if self._gap_notices:
                    self._flush_pending(pending)
                    pending = []
                    # Coalesce per reason: a sustained stall drops thousands of
                    # events, and one file record and one log line per event
                    # would bury the incident in its own noise. The count is
                    # what matters, not one entry each.
                    totals: dict[str, int] = {}
                    while self._gap_notices:
                        n, reason = self._gap_notices.popleft()
                        totals[reason] = totals.get(reason, 0) + n
                    for reason, n in totals.items():
                        self._record_gap(n, reason)

                # Write on EVERY pass, unconditionally. Batching comes from the
                # drain loop above collecting whatever is already queued -- not
                # from withholding writes until some threshold. An earlier version
                # only flushed at ARCHIVE_BATCH_MAX rows or an idle tick, and at
                # 2 Hz neither fires: inter-arrival (~570 ms) beats the 1 s idle
                # timeout, so rows sat unwritten in memory indefinitely and would
                # have died with the process. write() here is buffered and cheap;
                # fsync is what costs, and that stays on its own interval below.
                if pending:
                    self._flush_pending(pending)
                    pending = []

                now = time.monotonic()
                if now - last_fsync >= ARCHIVE_FSYNC_INTERVAL_S or stop:
                    for sp in self._spools.values():
                        sp.flush(force=True)
                    last_fsync = now
                    self._last_fsync_at = time.time()

                self._close_finished_hours(force=stop)

                if stop:
                    break
        except BaseException:
            log.exception("archive writer thread died")
            self._errors += 1
        finally:
            try:
                self._flush_pending(pending)
                for sp in list(self._spools.values()):
                    sp.close_clean()
                self._spools.clear()
            except Exception:  # pragma: no cover
                log.exception("archive: error sealing spool files on shutdown")
            log.info("archive writer thread stopped (rows_written=%d)", self._rows_written)

    def _spool_for(self, node_id: int, hour_key: str) -> _Spool:
        key = (node_id, hour_key)
        sp = self._spools.get(key)
        if sp is None:
            path = self._spool_dir / f"node={node_id}" / f"{hour_key}.ndjson"
            sp = _Spool(path, node_id, hour_key)
            self._spools[key] = sp
            for n, reason in self._orphan_gaps:
                sp.write_gap(n, reason)
            self._orphan_gaps.clear()
        return sp

    def _flush_pending(self, pending: list[dict]) -> None:
        if not pending:
            return
        by_target: dict[tuple[int, str], list[dict]] = {}
        for row in pending:
            node_id = row.get("node_id")
            if node_id is None:
                continue
            hour_key = _hour_key(datetime.fromisoformat(row["ts"]))
            by_target.setdefault((node_id, hour_key), []).append(row)

        for (node_id, hour_key), rows in by_target.items():
            try:
                sp = self._spool_for(node_id, hour_key)
                sp.write_rows(rows)
                sp.seq_gaps += self._count_seq_gaps(node_id, rows)
                self._rows_written += len(rows)
                self._last_row_at = rows[-1]["ts"]
            except Exception:
                self._errors += 1
                log.exception("archive: failed writing %d rows for node %s hour %s",
                              len(rows), node_id, hour_key)

    def _count_seq_gaps(self, node_id: int, rows: list[dict]) -> int:
        """Informational only -- a seq gap is usually RF loss, not our loss.

        Recorded in the manifest so a reader can see it, but deliberately does NOT
        clear `complete`: nothing the backend can do would have caught a frame
        that never arrived (protocol-spec section 6).

        SPANS RESTARTS: ``self._last_seq`` is seeded at startup by
        ``_recover_last_seq`` from the most recently sealed manifest, so a gap
        landing exactly on a restart boundary is still counted here, not just a
        gap within one continuous run. Found live 2026-09-04, before that seeding
        existed: a two-restart session left a genuine 5-frame gap at the
        part-0000/part-0001 seam that went uncounted because a fresh process has
        no memory of the last row the previous one wrote. Either way this never
        affects `complete` -- a restart-boundary gap is already outside what the
        backend could have observed, same as ordinary RF loss.
        """
        gaps = 0
        for row in rows:
            seq = row.get("seq_num")
            if seq is None:
                continue
            prev = self._last_seq.get(node_id)
            if prev is not None:
                step = (seq - prev) % SEQ_SPACE
                if step > 1:
                    gaps += 1
            self._last_seq[node_id] = seq
        return gaps

    def _record_gap(self, dropped: int, reason: str) -> None:
        """Mark every currently-open hour as holed. We cannot know which one lost
        the events -- they never reached us -- so taint them all rather than guess."""
        if not self._spools:
            # Nothing open yet -- hold it and taint the next spool created, so a
            # drop during a silent period is not simply forgotten.
            self._orphan_gaps.append((dropped, reason))
            return
        for sp in self._spools.values():
            sp.write_gap(dropped, reason)

    def _close_finished_hours(self, force: bool = False) -> None:
        now = datetime.now(timezone.utc)
        for key, sp in list(self._spools.items()):
            done = now >= _hour_start(sp.hour_key) + timedelta(hours=1, seconds=ARCHIVE_HOUR_GRACE_S)
            if not (done or force):
                continue
            try:
                sp.close_clean()
                del self._spools[key]
                self._compact(sp.path, sp.node_id, sp.hour_key)
            except Exception:
                self._errors += 1
                log.exception("archive: failed closing hour %s for node %s",
                              sp.hour_key, sp.node_id)

    # -- compaction ------------------------------------------------------ #
    def _compact(self, spool_path: Path, node_id: int, hour_key: str) -> None:
        """Seal one spool file as a new PART of its hour. Never overwrites.

        An hour can be sealed more than once: shutdown force-closes whatever hour
        is currently open even though it has not finished yet (see
        ``_close_finished_hours``), a fresh spool for that same hour is opened
        after the next start, and it gets its own seal when the hour later ends
        or the process stops again. A found-by-test bug wrote every seal to the
        same fixed key ``part-0000.ndjson.gz``: the second seal silently
        overwrote the first, destroying already-durable rows (219 written, then
        67 survived, then 61 -- discovered via scripts/verify_archive.py finding
        readings the database had that the archive did not).

        Fix: each seal becomes its own numbered part
        (``part-0000.ndjson.gz``, ``part-0001.ndjson.gz``, ...), and the hour's
        manifest is read-modify-write -- the existing manifest (if any) is read
        back via ``store.get_bytes`` and the new part is APPENDED to its
        ``parts`` list, never replacing what was already certified. Every prior
        part's stats stay exactly as they were: this can only add a part, never
        touch or reinterpret an existing one.

        Part index comes from the existing manifest, not from listing the
        store -- ``ArchiveStore`` only promises get/put, no listing, so this
        works unchanged against a future object-store backend that has no cheap
        directory listing.

        Ordering is still the durability contract: each part's data object is
        stored (and fsynced) before the manifest that certifies it, so a crash
        in between leaves a part with data and no manifest entry -- uncertified,
        therefore not deletable, never certifying data that was never stored.
        """
        stats = scan_spool_file(spool_path)
        part_complete = stats["gaps"] == 0 and stats["closed"] and not stats["truncated"]

        manifest_key = _partition_key(node_id, hour_key, "_manifest.json")
        existing = self._store.get_bytes(manifest_key)
        manifest = json.loads(existing) if existing else None
        parts: list[dict] = list(manifest["parts"]) if manifest else []
        part_index = len(parts)
        part_name = f"part-{part_index:04d}.ndjson.gz"

        with tempfile.TemporaryDirectory(prefix="noventis-compact-") as td:
            gz = Path(td) / part_name
            with open(spool_path, "rb") as src, gzip.open(gz, "wb", compresslevel=6) as dst:
                shutil.copyfileobj(src, dst)

            data_key = _partition_key(node_id, hour_key, part_name)
            self._store.put(gz, data_key)

            parts.append({
                "key": data_key,
                "rows": stats["rows"],
                "drops": stats["gaps"],
                "seq_gaps": stats["seq_gaps"],
                "sealed": stats["closed"],
                "truncated": stats["truncated"],
                "complete": part_complete,
                "bytes_gz": gz.stat().st_size,
                "sha256": sha256_file(gz),
                # Last seq_num this part wrote for this node, restored into
                # self._last_seq at the next startup (see _recover_last_seq) so
                # gap detection spans a restart instead of resetting to unknown.
                # Fixes a found-live blind spot: a genuine 5-frame gap that fell
                # exactly on a restart boundary went uncounted because
                # self._last_seq is in-memory state that a fresh process starts
                # without (verified 2026-09-04 against the real node).
                "last_seq": self._last_seq.get(node_id),
                "closed_at": datetime.now(timezone.utc).isoformat(),
            })

            # Aggregate over every part sealed for this hour so far. A prior
            # part's own complete=false (a drop, a crash) can never be undone by
            # a later, cleaner part -- one bad part taints the whole hour.
            agg_complete = all(p["complete"] for p in parts)
            manifest_out = {
                "schema": 2,
                "node_id": node_id,
                "hour": hour_key,
                "rows_written": sum(p["rows"] for p in parts),
                "drops_observed": sum(p["drops"] for p in parts),
                "seq_gaps_observed": sum(p["seq_gaps"] for p in parts),
                "sealed": all(p["sealed"] for p in parts),
                "truncated": any(p["truncated"] for p in parts),
                # "no loss OBSERVED BY THE BACKEND" -- see the module docstring.
                # A backend restart mid-hour is itself a window the archive
                # cannot see into (no code runs while the process is down), so
                # more than one part is flagged rather than silently merged as
                # if the hour were one continuous, gapless capture.
                "complete": agg_complete,
                "multi_part": len(parts) > 1,
                "parts": parts,
                "closed_at": datetime.now(timezone.utc).isoformat(),
            }
            mpath = Path(td) / "_manifest.json"
            mpath.write_text(json.dumps(manifest_out, indent=2), encoding="utf-8")
            self._store.put(mpath, manifest_key)

        if not part_complete:
            self._incomplete_hours += 1
        self._segments += 1
        spool_path.unlink(missing_ok=True)
        fsync_dir(spool_path.parent)
        log.info(
            "archive: sealed node=%s hour=%s part=%d rows=%d part_complete=%s "
            "hour_complete=%s (%d bytes gz)",
            node_id, hour_key, part_index, stats["rows"], part_complete,
            manifest_out["complete"], parts[-1]["bytes_gz"],
        )

    def _recover_orphans(self) -> None:
        """Compact spool files left by a previous run.

        Any file still here at startup belongs to a process that is gone. If it
        has no ``_close`` record it stopped uncleanly, and ``_compact`` marks it
        incomplete -- durable taint for a crash that would have erased an
        in-memory counter.
        """
        for path in sorted(self._spool_dir.glob("node=*/*.ndjson")):
            try:
                node_id = int(path.parent.name.split("=", 1)[1])
                hour_key = path.stem
                stats = scan_spool_file(path)
                log.info("archive: recovering orphaned spool %s (rows=%d sealed=%s)",
                         path.name, stats["rows"], stats["closed"])
                self._compact(path, node_id, hour_key)
            except Exception:
                self._errors += 1
                log.exception("archive: could not recover orphaned spool %s", path)

    def _recover_last_seq(self) -> None:
        """Seed ``self._last_seq`` from the most recently sealed manifest per
        node, so gap detection spans a restart instead of starting blind.

        Without this, a restart resets ``self._last_seq`` to empty, and the
        gap between the last row before shutdown and the first row after it goes
        uncounted -- found live 2026-09-04: a two-restart session left a genuine
        5-frame gap exactly on a restart boundary that seq_gaps_observed missed.
        This does not change ``complete`` semantics (that gap was already outside
        what the backend could observe either way); it only makes the informational
        counter, and the ``last_seq`` this method restores, honest about it.

        LOCAL-DISK ONLY, by necessity: ``ArchiveStore`` deliberately offers no
        ``list()`` (see ``_compact``'s docstring on part numbering), and finding
        "the newest manifest across every hour for this node" needs either a
        listing or an index this project does not have yet. A future non-local
        store needs a small index (e.g. one `_last_seq.json` per node, written
        alongside each manifest) to keep this working without a directory scan.
        """
        data_dir = self._dir / "data"
        if not data_dir.is_dir():
            return
        newest: dict[int, tuple[str, int]] = {}   # node_id -> (closed_at, seq)
        for mpath in data_dir.rglob("_manifest.json"):
            try:
                m = json.loads(mpath.read_text(encoding="utf-8"))
                parts = m.get("parts")
                if not parts:
                    continue                       # schema 1, or an empty hour
                last_seq = parts[-1].get("last_seq")
                if last_seq is None:
                    continue
                node_id = m["node_id"]
                closed_at = m.get("closed_at", "")
                if node_id not in newest or closed_at > newest[node_id][0]:
                    newest[node_id] = (closed_at, last_seq)
            except Exception:
                log.warning("archive: could not read %s while recovering last_seq", mpath)
        for node_id, (closed_at, seq) in newest.items():
            self._last_seq[node_id] = seq
            log.info("archive: restored last_seq=%d for node %s from %s",
                     seq, node_id, closed_at)

    # -- introspection (GET /health) ------------------------------------- #
    def status(self) -> dict:
        spool_bytes = 0
        oldest_open = None
        for sp in list(self._spools.values()):
            try:
                spool_bytes += sp.path.stat().st_size
            except OSError:  # pragma: no cover
                pass
            if oldest_open is None or sp.hour_key < oldest_open:
                oldest_open = sp.hour_key
        return {
            "running": self._thread is not None and self._thread.is_alive(),
            "subscribed": self._task is not None and not self._task.done(),
            "dir": str(self._dir),
            "queued": self._q.qsize(),
            "queue_capacity": ARCHIVE_QUEUE_MAX,
            "rows_queued": self._rows_queued,
            "rows_written": self._rows_written,
            "last_row_at": self._last_row_at,
            "segments_sealed": self._segments,
            "incomplete_hours": self._incomplete_hours,
            "bus_drops": self._bus_drops_seen,
            "queue_drops": self._queue_drops,
            "errors": self._errors,
            "open_hours": len(self._spools),
            "oldest_open_hour": oldest_open,
            "spool_bytes": spool_bytes,
            "last_fsync_at": self._last_fsync_at,
            "fsync_interval_s": ARCHIVE_FSYNC_INTERVAL_S,
        }
