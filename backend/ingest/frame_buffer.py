"""
backend/ingest/frame_buffer.py -- bounded in-memory ring of recent candidate frames.

Replaces the old "write every frame to the raw_frames table" model. The forensic
value of a *successful* frame is near-zero (it decoded fine; its values are
already in `readings`), and the only question raw frames answer -- "what just came
off the wire?" -- is a minutes-old question, not a days-old one.

So: the most recent ``RAW_FRAME_BUFFER_SIZE`` frames (CRC pass *and* fail) live
here, in a ``collections.deque`` with a fixed ``maxlen`` -- oldest entries evict
automatically. This buffer is **process-lifetime only**: it is intentionally NOT
persisted and is empty again after a restart. Inspect it via ``GET /debug/frames``.

CRC-*failed* frames are still written to the ``raw_frames`` table by
``db/writer.py`` -- those are rare (~12 per 24k in practice), stay tiny
indefinitely, and are the only frames with long-term diagnostic value.

Like ``db/writer.py`` and ``ws/manager.py`` this is an **independent** event-bus
subscriber (its own ``bus.subscribe()`` iterator). It never imports pyserial.
Started/stopped as an asyncio task by ``main.py``'s lifespan.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections import deque
from datetime import datetime
from typing import NamedTuple

log = logging.getLogger(__name__)

# ~40 min of headroom at the ~2 Hz per-node transmit rate. Override with
# NOVENTIS_RAW_FRAME_BUFFER_SIZE; this is the one knob for the buffer.
RAW_FRAME_BUFFER_SIZE = int(os.getenv("NOVENTIS_RAW_FRAME_BUFFER_SIZE", "5000"))


class BufferedFrame(NamedTuple):
    """One entry in the ring -- the same fields the raw_frames table used to hold,
    plus a monotonic ``seq`` so callers have a stable id / sort key."""

    seq: int
    node_id: int | None
    received_at: datetime
    crc_ok: bool
    raw: bytes


class FrameRingBuffer:
    """Fixed-capacity ring of the most recent frames off the wire.

    ``append`` is safe to call from anywhere; reads (`recent`) copy under the
    GIL-atomic deque semantics we rely on elsewhere. Doubles as an event-bus
    subscriber: ``start()`` spawns a task that funnels every ``FrameEvent`` into
    the ring.
    """

    def __init__(self, bus=None, capacity: int = RAW_FRAME_BUFFER_SIZE) -> None:
        self._bus = bus
        self._buf: deque[BufferedFrame] = deque(maxlen=capacity)
        self._seq = 0
        self._received_total = 0
        self._task: asyncio.Task | None = None

    # -- write ---------------------------------------------------------------- #
    def append(self, *, node_id: int | None, received_at: datetime,
               crc_ok: bool, raw: bytes) -> None:
        self._seq += 1
        self._received_total += 1
        self._buf.append(BufferedFrame(self._seq, node_id, received_at, crc_ok, bytes(raw)))

    def record_event(self, event: dict) -> None:
        self.append(
            node_id=event.get("node_id"),
            received_at=event["received_at"],
            crc_ok=event["crc_ok"],
            raw=event["raw"],
        )

    # -- read --------------------------------------------------------------- #
    def recent(self, limit: int, crc_ok: bool | None = None,
               node_id: int | None = None) -> list[BufferedFrame]:
        """Up to ``limit`` most recent frames, newest first, optionally filtered
        by ``crc_ok`` and/or ``node_id``."""
        out: list[BufferedFrame] = []
        for f in reversed(self._buf):
            if crc_ok is not None and f.crc_ok is not crc_ok:
                continue
            if node_id is not None and f.node_id != node_id:
                continue
            out.append(f)
            if len(out) >= limit:
                break
        return out

    # -- lifecycle (event-bus subscriber) --------------------------------- #
    def start(self) -> None:
        if self._bus is not None and (self._task is None or self._task.done()):
            self._task = asyncio.create_task(self._run(), name="frame-ring-buffer")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def _run(self) -> None:
        log.info("frame ring buffer subscribed to event bus (capacity=%d)", self._buf.maxlen)
        async for event in self._bus.subscribe("frame-buffer"):
            try:
                self.record_event(event)
            except Exception:  # pragma: no cover - a bad event must not kill the loop
                log.exception("frame ring buffer: failed to record event")

    # -- introspection (GET /health, GET /debug/frames) ----------------- #
    def status(self) -> dict:
        return {
            "size": len(self._buf),
            "capacity": self._buf.maxlen,
            "received_total": self._received_total,
            "running": self._task is not None and not self._task.done(),
        }
