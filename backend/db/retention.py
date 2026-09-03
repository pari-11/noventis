"""
backend/db/retention.py -- readings rollup + retention, wired into main.py's
lifespan as a periodic task (hourly by default).

The `readings` table grows at ~2 rows/sec/node forever. This job keeps only the
last ``READINGS_FULL_RES_HOURS`` at full resolution; everything older is
aggregated into ``ROLLUP_INTERVAL_MINUTES`` time buckets per node
(``readings_rollup``) and the original rows are deleted.

Design points:
  * **avg + min + max**, not avg alone. Averaging a one-minute bucket of IMU data
    erases the short accel/gyro spikes that are the whole point of that signal;
    min/max preserves "did something happen this minute" for negligible cost.
  * **Single tier.** No coarser rollup on top of the buckets. At
    ~1,440 rows/day/node post-rollup this stays small, and a second tier would
    only add merge complexity to the history API.
  * **Batched.** Old data is processed in ``BACKLOG_BATCH_HOURS``-wide windows,
    one transaction each, so a large first-run backlog never holds a write lock
    long enough to stall live ingestion. The first pass drains the *entire*
    existing backlog this way, not just newly-aged data.
  * **Bucket-aligned cutoff.** The retention cutoff is floored to a bucket
    boundary, so a bucket is always entirely inside or entirely outside the
    window -- no bucket is ever rolled up in two pieces.
  * **Vacuum:** ``PRAGMA auto_vacuum = INCREMENTAL`` + periodic
    ``PRAGMA incremental_vacuum`` after each pass, never a full ``VACUUM`` in the
    loop (a full VACUUM rewrites the whole file and needs near-exclusive access).
    auto_vacuum mode can only be changed by a VACUUM, so switching the existing
    DB costs one deliberate full VACUUM -- done once, in ``_prepare``.
  * **No destructive side effects at boot.** ``_prepare`` only converts the
    vacuum mode. It never deletes rows. The one-off cleanup of legacy CRC-valid
    ``raw_frames`` rows is a separate operator-run script
    (``scripts/migrate_purge_legacy_frames.py``), so restoring an old backup can
    never trigger a silent purge.

All thresholds are module constants at the top, each with an env override.
``READINGS_FULL_RES_HOURS`` defaults to 168 (7 days) -- raise it for a longer
full-resolution retention window (linear storage cost).
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from .session import SessionLocal, engine

log = logging.getLogger(__name__)

# -- thresholds ------------------------------------------------------------- #
# How far back full-resolution (~0.5 s / 2 Hz) readings stay queryable before
# they are collapsed into ROLLUP_INTERVAL_MINUTES buckets. This is THE knob to
# tune for production: raise it if operators need fine-grained history further
# back, at a linear storage cost (~1.2M rows/week/node at 2 Hz; see the storage
# note in docs/architecture.md). 168 h = 7 days.
READINGS_FULL_RES_HOURS = int(os.getenv("NOVENTIS_READINGS_FULL_RES_HOURS", "168"))
# Bucket width for the rollup. Should divide 60 evenly (1, 2, 3, 5, 10, 15, 30, 60).
ROLLUP_INTERVAL_MINUTES = int(os.getenv("NOVENTIS_ROLLUP_INTERVAL_MINUTES", "1"))
# How often a retention pass runs.
RETENTION_INTERVAL_S = float(os.getenv("NOVENTIS_RETENTION_INTERVAL_S", "3600"))
# Width of one backlog window (one transaction). Keeps the write lock short.
BACKLOG_BATCH_HOURS = int(os.getenv("NOVENTIS_BACKLOG_BATCH_HOURS", "1"))
# Pages to hand back to the OS per incremental_vacuum call.
INCREMENTAL_VACUUM_PAGES = int(os.getenv("NOVENTIS_INCREMENTAL_VACUUM_PAGES", "2000"))
# Wait this long after startup before the first pass, so booting isn't slowed.
RETENTION_STARTUP_DELAY_S = float(os.getenv("NOVENTIS_RETENTION_STARTUP_DELAY_S", "3"))

# Source reading columns -> aggregated into <col>_avg / <col>_min / <col>_max.
_AGG_COLS = ("tof_mm", "accel_x", "accel_y", "accel_z", "gyro_x", "gyro_y", "gyro_z")


def _sqlts(dt: datetime) -> str:
    """A tz-aware datetime -> the naive-UTC string format the DB stores."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _parse_ts(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)


def _floor_to_bucket(dt: datetime) -> datetime:
    bsec = ROLLUP_INTERVAL_MINUTES * 60
    epoch = int(dt.astimezone(timezone.utc).timestamp())
    return datetime.fromtimestamp(epoch - (epoch % bsec), tz=timezone.utc)


def _rollup_insert_sql() -> str:
    """INSERT ... SELECT that aggregates one window of `readings` into buckets."""
    bsec = ROLLUP_INTERVAL_MINUTES * 60
    target_cols = ["node_id", "bucket_start", "sample_count"]
    select_exprs = [
        "node_id",
        f"datetime((CAST(strftime('%s', timestamp) AS INTEGER) / {bsec}) * {bsec}, "
        "'unixepoch') AS bucket_start",
        "COUNT(*)",
    ]
    conflict_sets = []
    for c in _AGG_COLS:
        target_cols += [f"{c}_avg", f"{c}_min", f"{c}_max"]
        select_exprs += [f"AVG({c})", f"MIN({c})", f"MAX({c})"]
        for suffix in ("avg", "min", "max"):
            conflict_sets.append(f"{c}_{suffix} = excluded.{c}_{suffix}")
    conflict_sets.append("sample_count = excluded.sample_count")
    return (
        f"INSERT INTO readings_rollup ({', '.join(target_cols)})\n"
        f"SELECT {', '.join(select_exprs)}\n"
        "FROM readings\n"
        "WHERE timestamp >= :start AND timestamp < :end\n"
        "GROUP BY node_id, bucket_start\n"
        "ON CONFLICT(node_id, bucket_start) DO UPDATE SET\n  "
        + ",\n  ".join(conflict_sets)
    )


class RetentionJob:
    def __init__(self, session_factory=None, *, interval_s: float = RETENTION_INTERVAL_S):
        self._session_factory = session_factory or SessionLocal
        self._interval_s = interval_s
        self._task: asyncio.Task | None = None
        self._insert_sql = text(_rollup_insert_sql())

        self._passes = 0
        self._buckets_written = 0
        self._readings_rolled_up = 0
        self._errors = 0
        self._last_pass_at: datetime | None = None
        self._last_pass_secs = 0.0
        self._auto_vacuum_mode = "unknown"

    # -- lifecycle ------------------------------------------------------- #
    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="retention")

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
            "passes": self._passes,
            "buckets_written": self._buckets_written,
            "readings_rolled_up": self._readings_rolled_up,
            "errors": self._errors,
            "last_pass_at": self._last_pass_at.isoformat() if self._last_pass_at else None,
            "last_pass_secs": round(self._last_pass_secs, 3),
            "auto_vacuum_mode": self._auto_vacuum_mode,
            "full_res_hours": READINGS_FULL_RES_HOURS,
            "rollup_interval_minutes": ROLLUP_INTERVAL_MINUTES,
        }

    # -- loop ---------------------------------------------------------- #
    async def _run(self) -> None:
        log.info(
            "retention job started (keep %dh full-res, %d-min buckets, every %.0fs)",
            READINGS_FULL_RES_HOURS, ROLLUP_INTERVAL_MINUTES, self._interval_s,
        )
        try:
            await self._prepare()
        except asyncio.CancelledError:
            raise
        except Exception:
            self._errors += 1
            log.exception("retention: one-time setup failed; periodic passes will still run")

        if RETENTION_STARTUP_DELAY_S:
            await asyncio.sleep(RETENTION_STARTUP_DELAY_S)

        while True:
            try:
                await self._pass()
            except asyncio.CancelledError:
                raise
            except Exception:
                self._errors += 1
                log.exception("retention: pass failed")
            await asyncio.sleep(self._interval_s)

    # -- one-time setup --------------------------------------------------- #
    async def _prepare(self) -> None:
        # NOTE: the backend never deletes data as a side effect of booting.
        # Purging legacy CRC-valid `raw_frames` rows (left by the pre-ring-buffer
        # storage model) is a deliberate operator action --
        # `python scripts/migrate_purge_legacy_frames.py`, which prompts first.
        await self._ensure_incremental_autovacuum()

    async def _ensure_incremental_autovacuum(self) -> None:
        async with engine.connect() as conn:
            conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
            mode = (await conn.exec_driver_sql("PRAGMA auto_vacuum")).scalar()
            if mode == 2:  # already INCREMENTAL
                self._auto_vacuum_mode = "incremental"
                return
            try:
                await conn.exec_driver_sql("PRAGMA auto_vacuum=INCREMENTAL")
                await conn.exec_driver_sql("VACUUM")  # required for the change to take
                self._auto_vacuum_mode = "incremental"
                log.info(
                    "retention setup: converted DB to auto_vacuum=INCREMENTAL "
                    "(one-time full VACUUM)"
                )
            except Exception:
                self._auto_vacuum_mode = f"conversion-failed (was mode={mode})"
                log.exception(
                    "retention setup: could not switch to incremental auto_vacuum; "
                    "incremental_vacuum passes will be no-ops (deletes still free "
                    "pages for reuse, the file just won't shrink)"
                )

    async def _incremental_vacuum(self) -> None:
        try:
            async with engine.connect() as conn:
                conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
                await conn.exec_driver_sql(
                    f"PRAGMA incremental_vacuum({INCREMENTAL_VACUUM_PAGES})"
                )
        except Exception:
            log.exception("retention: incremental_vacuum failed (non-fatal)")

    # -- a pass ------------------------------------------------------- #
    async def _pass(self) -> None:
        started = asyncio.get_running_loop().time()
        cutoff = _floor_to_bucket(datetime.now(timezone.utc) - timedelta(hours=READINGS_FULL_RES_HOURS))

        async with self._session_factory() as session:
            oldest = (await session.execute(
                text("SELECT MIN(timestamp) FROM readings WHERE timestamp < :cutoff"),
                {"cutoff": _sqlts(cutoff)},
            )).scalar()

        if oldest is None:
            self._passes += 1
            self._last_pass_at = datetime.now(timezone.utc)
            self._last_pass_secs = asyncio.get_running_loop().time() - started
            log.debug("retention pass: nothing older than %s", _sqlts(cutoff))
            return

        window_start = _floor_to_bucket(_parse_ts(oldest))
        batch = timedelta(hours=BACKLOG_BATCH_HOURS)
        buckets = rolled = windows = 0

        while window_start < cutoff:
            window_end = min(window_start + batch, cutoff)
            b, d = await self._rollup_window(window_start, window_end)
            buckets += b
            rolled += d
            windows += 1
            window_start = window_end

        self._passes += 1
        self._buckets_written += buckets
        self._readings_rolled_up += rolled
        self._last_pass_at = datetime.now(timezone.utc)
        self._last_pass_secs = asyncio.get_running_loop().time() - started

        if rolled:
            await self._incremental_vacuum()
            log.info(
                "retention pass: rolled up %d readings into %d buckets across %d "
                "window(s), deleted the originals older than %s (%.2fs)",
                rolled, buckets, windows, _sqlts(cutoff), self._last_pass_secs,
            )

    async def _rollup_window(self, start: datetime, end: datetime) -> tuple[int, int]:
        """Aggregate + delete [start, end) atomically. Returns (buckets, rows)."""
        params = {"start": _sqlts(start), "end": _sqlts(end)}
        async with self._session_factory() as session:
            async with session.begin():
                ins = await session.execute(self._insert_sql, params)
                deleted = await session.execute(
                    text("DELETE FROM readings WHERE timestamp >= :start AND timestamp < :end"),
                    params,
                )
        return (max(ins.rowcount, 0), max(deleted.rowcount, 0))
