"""
backend/ingest/event_bus.py -- in-process pub/sub fan-out.

One publisher (the serial reader) -> many independent subscribers
(db.writer, ws.manager, ...). Each subscriber gets its own bounded asyncio.Queue
so a slow consumer cannot block the others; on overflow the oldest event for that
subscriber is dropped (with a warning) rather than stalling ingestion.

This abstraction is deliberately tiny. When Noventis outgrows a single process,
swap the internals for Redis pub/sub / NATS / etc. -- subscribers should not need
to change.

RULES:
  - Subscribers MUST NOT import pyserial or open the serial port. They only
    consume events from here.
  - The event payload is whatever serial_reader publishes; see FrameEvent below
    (currently a plain dict -- promote to a typed model when it stabilises).

TODO:
  - [ ] Replace the dict payload with a frozen dataclass / pydantic model.
  - [ ] Optional: topic filtering (per-node) at the bus instead of per-subscriber.
  - [ ] Metrics: dropped-event counter per subscriber.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

log = logging.getLogger(__name__)

# Shape of an event on the bus (documented here until it becomes a real type):
#   {
#     "raw":         bytes,      # exact wire bytes
#     "crc_ok":      bool,
#     "node_id":     int | None, # None if header unparseable
#     "seq_num":     int | None,
#     "values":      dict,       # decoded TLV keys; {} when crc_ok is False
#     "received_at": datetime,   # timezone-aware UTC
#   }
FrameEvent = dict


class EventBus:
    def __init__(self, maxsize: int = 1000) -> None:
        self._subscribers: set[asyncio.Queue] = set()
        self._maxsize = maxsize

    async def publish(self, event: FrameEvent) -> None:
        for q in list(self._subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                try:
                    q.get_nowait()  # drop oldest for this lagging subscriber
                except asyncio.QueueEmpty:
                    pass
                try:
                    q.put_nowait(event)
                except asyncio.QueueFull:
                    pass
                log.warning("event-bus subscriber lagging; dropped an event")

    @asynccontextmanager
    async def subscribe(self) -> AsyncIterator[asyncio.Queue]:
        """Context manager yielding a dedicated queue; auto-unsubscribes on exit."""
        q: asyncio.Queue = asyncio.Queue(maxsize=self._maxsize)
        self._subscribers.add(q)
        try:
            yield q
        finally:
            self._subscribers.discard(q)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)


# Module-level singleton shared by the reader and all consumers.
bus = EventBus()
