"""
backend/ingest/event_bus.py -- in-process pub/sub fan-out.

One publisher (the serial reader thread, via ``main.py``'s
``loop_safe_publisher``) -> many independent subscribers (``db.writer``,
``ws.manager``, ...). Every subscriber receives every event -- this is fan-out,
not a work queue with competing consumers.

Contract:
  * ``publish(event)`` is **synchronous** and non-blocking, so it can be invoked
    directly from ``loop.call_soon_threadsafe``. It pushes ``event`` onto each
    subscriber's private ``asyncio.Queue``.
  * ``subscribe()`` returns an async iterator; ``async for event in
    bus.subscribe(): ...``. The subscriber's queue is registered synchronously
    when ``subscribe()`` is called (no missed events between call and first
    iteration) and unregistered when the iterator is closed (task cancelled,
    loop broken, or garbage-collected).
  * Each queue is bounded (default 10k -- ~80 min at the ~2 Hz node rate). If a
    subscriber falls that far behind, its oldest event is dropped and counted
    rather than blocking ingestion or growing without bound.

Subscribers MUST NOT import pyserial or open the serial port -- they only ever
see events from here.

Event shape: see ``serial_reader.SerialReader._handle_frame`` --
``{node_id, seq_num, crc_ok, raw, raw_hex, values, received_at}``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator

log = logging.getLogger(__name__)

FrameEvent = dict  # TODO: promote to a frozen dataclass once the shape is stable


class EventBus:
    def __init__(self, maxsize: int = 10_000) -> None:
        self._subscribers: set[asyncio.Queue] = set()
        self._maxsize = maxsize
        self._published = 0
        self._dropped = 0

    # -- producer side ------------------------------------------------------ #
    def publish(self, event: FrameEvent) -> None:
        """Fan ``event`` out to every subscriber. Synchronous and non-blocking."""
        self._published += 1
        for q in list(self._subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                try:
                    q.get_nowait()          # drop this subscriber's oldest
                except asyncio.QueueEmpty:  # pragma: no cover
                    pass
                try:
                    q.put_nowait(event)
                except asyncio.QueueFull:   # pragma: no cover
                    pass
                self._dropped += 1
                log.warning(
                    "event-bus subscriber lagging; dropped oldest event (dropped=%d)",
                    self._dropped,
                )

    # -- consumer side --------------------------------------------------- #
    def subscribe(self) -> AsyncIterator[FrameEvent]:
        """Register a subscriber and return an async iterator over its events."""
        queue: asyncio.Queue = asyncio.Queue(maxsize=self._maxsize)
        self._subscribers.add(queue)
        return self._stream(queue)

    async def _stream(self, queue: asyncio.Queue) -> AsyncIterator[FrameEvent]:
        try:
            while True:
                yield await queue.get()
        finally:
            self._subscribers.discard(queue)

    # -- introspection (GET /health, logs) ----------------------------- #
    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def status(self) -> dict:
        return {
            "subscribers": len(self._subscribers),
            "published": self._published,
            "dropped": self._dropped,
        }


# Module-level singleton shared by the reader bridge and all consumers.
bus = EventBus()


if __name__ == "__main__":
    async def _demo():
        b = EventBus()

        async def consumer(name, n):
            got = []
            async for event in b.subscribe():
                got.append(event["seq_num"])
                if len(got) == n:
                    break
            print(f"  {name} received seq {got}")
            return got

        c1 = asyncio.create_task(consumer("A", 3))
        c2 = asyncio.create_task(consumer("B", 3))
        await asyncio.sleep(0)  # let both register
        assert b.subscriber_count == 2, b.status()
        for seq in range(3):
            b.publish({"seq_num": seq, "crc_ok": True})
        a, bb = await asyncio.gather(c1, c2)
        assert a == bb == [0, 1, 2], (a, bb)
        await asyncio.sleep(0)
        assert b.subscriber_count == 0, b.status()
        print("event_bus.py self-test OK:", b.status())

    asyncio.run(_demo())
