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
  * Drops are counted **per subscriber** as well as in total, and named:
    ``subscribe("db-writer")``. A single global counter says something was lost
    but not by whom, and that distinction matters -- a drop on ws.manager is a
    cosmetic gap in a chart, while a drop on a durability-critical subscriber
    means an archive window can no longer be certified complete. ``status()``
    reports the breakdown; ``dropped_for(name)`` reads one.

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


class _Subscriber:
    """One subscriber's queue plus its own counters.

    Drops are counted **per subscriber**, not just globally: a single total tells
    you something was lost but not by whom, and the answer changes what you do.
    The archive tier treats its own drop count as a durability signal (an hour
    that saw one can never be certified complete), so the count has to be
    attributable, not aggregate.
    """

    __slots__ = ("queue", "name", "delivered", "dropped")

    def __init__(self, queue: asyncio.Queue, name: str) -> None:
        self.queue = queue
        self.name = name
        self.delivered = 0
        self.dropped = 0


class EventBus:
    def __init__(self, maxsize: int = 10_000) -> None:
        self._subscribers: dict[asyncio.Queue, _Subscriber] = {}
        self._maxsize = maxsize
        self._published = 0
        self._dropped = 0
        self._anon = 0

    # -- producer side ------------------------------------------------------ #
    def publish(self, event: FrameEvent) -> None:
        """Fan ``event`` out to every subscriber. Synchronous and non-blocking."""
        self._published += 1
        for sub in list(self._subscribers.values()):
            q = sub.queue
            try:
                q.put_nowait(event)
                sub.delivered += 1
            except asyncio.QueueFull:
                try:
                    q.get_nowait()          # drop this subscriber's oldest
                except asyncio.QueueEmpty:  # pragma: no cover
                    pass
                try:
                    q.put_nowait(event)
                    sub.delivered += 1
                except asyncio.QueueFull:   # pragma: no cover
                    pass
                sub.dropped += 1
                self._dropped += 1
                log.warning(
                    "event-bus subscriber %r lagging; dropped oldest event "
                    "(this subscriber=%d, all subscribers=%d)",
                    sub.name, sub.dropped, self._dropped,
                )

    # -- consumer side --------------------------------------------------- #
    def subscribe(self, name: str | None = None) -> AsyncIterator[FrameEvent]:
        """Register a subscriber and return an async iterator over its events.

        ``name`` labels the subscriber in ``status()`` and in the lagging
        warning. Optional so existing callers keep working; pass one.
        """
        queue: asyncio.Queue = asyncio.Queue(maxsize=self._maxsize)
        if name is None:
            self._anon += 1
            name = f"anon-{self._anon}"
        self._subscribers[queue] = _Subscriber(queue, name)
        return self._stream(queue)

    async def _stream(self, queue: asyncio.Queue) -> AsyncIterator[FrameEvent]:
        try:
            while True:
                yield await queue.get()
        finally:
            self._subscribers.pop(queue, None)

    def dropped_for(self, name: str) -> int:
        """Total events dropped for the subscriber registered under ``name``."""
        for sub in self._subscribers.values():
            if sub.name == name:
                return sub.dropped
        return 0

    # -- introspection (GET /health, logs) ----------------------------- #
    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def status(self) -> dict:
        return {
            "subscribers": len(self._subscribers),
            "published": self._published,
            "dropped": self._dropped,
            "by_subscriber": {
                sub.name: {
                    "queued": sub.queue.qsize(),
                    "capacity": self._maxsize,
                    "delivered": sub.delivered,
                    "dropped": sub.dropped,
                }
                for sub in self._subscribers.values()
            },
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
        print("  fan-out OK:", b.status())

        # Drops are attributed to the subscriber that lagged, not just totalled.
        tiny = EventBus(maxsize=2)
        fast = tiny.subscribe("fast")          # drained below
        slow = tiny.subscribe("slow")          # never iterated -> queue fills
        for seq in range(5):
            tiny.publish({"seq_num": seq})
        st = tiny.status()
        assert st["by_subscriber"]["slow"]["dropped"] == 3, st
        assert st["by_subscriber"]["fast"]["dropped"] == 3, st
        assert tiny.dropped_for("slow") == 3, st
        assert st["dropped"] == 6, st       # total counts both
        # The lagging queue keeps its NEWEST events (oldest are the ones dropped).
        assert [tiny._subscribers[q].name for q in tiny._subscribers] == ["fast", "slow"]
        got = [(await anext(fast))["seq_num"], (await anext(fast))["seq_num"]]
        assert got == [3, 4], got
        await fast.aclose()
        await slow.aclose()
        print("  per-subscriber drop attribution OK:", tiny.status())
        print("event_bus.py self-test OK")

    asyncio.run(_demo())
