"""
backend/db/writer.py -- event-bus subscriber that persists frames.

Subscribes to event_bus.bus independently of ws.manager. Never imports pyserial.
Runs as an asyncio task started/stopped by main.py's lifespan.

Per received event:
  1. INSERT into raw_frames  (ALWAYS -- forensic log, CRC pass or fail).
  2. If crc_ok:
       - UPSERT nodes.last_seen = event.received_at
       - INSERT into readings with ON CONFLICT (node_id, seq_num) DO NOTHING
         (idempotent re-ingestion -- constraint #5).

TODO:
  - [ ] async run(stop_event): `async with bus.subscribe() as q: while ...: q.get()`
  - [ ] Use sqlalchemy.dialects.sqlite.insert(...).on_conflict_do_nothing(...).
  - [ ] Commit cadence: per-event to start; batch (N rows / 100 ms) if it can't
        keep up. WAL mode makes single-writer commits cheap-ish.
  - [ ] Error isolation: a malformed event must be logged and skipped, never
        kill the loop.
  - [ ] Map event["values"] -> Reading columns: `tof_mm` direct; flatten
        `accel_mss` -> accel_x/y/z and `gyro_rads` -> gyro_x/y/z.
"""

from __future__ import annotations


async def run(stop_event) -> None:
    raise NotImplementedError("writer.run(): consume event_bus.bus and persist frames")
