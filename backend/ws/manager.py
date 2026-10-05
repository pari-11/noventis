"""
backend/ws/manager.py -- WebSocket fan-out for GET /live.

Subscribes to the event bus **independently** of db.writer (its own
``bus.subscribe()`` iterator). Never imports pyserial.

Responsibilities:
  * Track active WebSocket connections, each with an optional ``node_id`` filter
    taken from the ``/live?node_id=`` query param (absent -> receive all nodes).
  * For every CRC-valid frame event, serialise it to JSON and hand it to the
    connections whose filter matches ``event["node_id"]`` (or have no filter).
  * Drop connections that error on send; the per-socket ``serve()`` coroutine
    also unregisters on disconnect.

CRC-invalid frames are NOT pushed here -- they are forensic-only and reachable
via GET /raw-frames.

**Per-connection outboxes.** The broadcast loop never awaits a socket. It used to:
``for ws in targets: await ws.send_text(...)``, which meant one client whose TCP
send buffer was full -- a suspended laptop, a phone on bad wifi, a throttled
background tab -- blocked the broadcast task and stalled the live feed *for every
other client*. Worse, while it was stuck this subscriber's own bus queue filled
and began discarding, so one slow client silently cost everyone data.

Now each connection owns a bounded ``asyncio.Queue`` and one send task.
Broadcast is a ``put_nowait`` per target and cannot block. A client that falls
behind overflows its own queue, drops its own oldest messages, and is told about
it; nobody else is affected.

Invariants worth keeping:
  * **One writer per socket.** Only that connection's send task calls
    ``ws.send_*``. ``serve()`` only *reads*. Two coroutines writing the same
    socket races on close and raises ``RuntimeError`` on a send after close.
  * **Gaps are announced.** A dropped message is a hole in the client's data. It
    is told (``{"type":"gap"}``) so the chart can break the line instead of
    drawing straight through the missing samples, which would read as real data.
  * **Pings only go to idle clients.** A client with a backlog is already
    provably alive; pushing a ping into its full queue would evict a real reading
    to prove something we already know.

Outbound messages. A **data** message has no ``type`` key (unchanged contract,
see docs/protocol-spec.md section 3); every **control** message has one, and
clients must ignore control types they do not recognise::

    {"node_id": 1, "seq_num": 4211, "ts": "2026-08-30T12:34:56.789+00:00",
     "values": {"tof_mm": 812, "accel_mss": [x,y,z], "gyro_rads": [x,y,z]}}

    {"type": "ready", "node_id": 1}       # handshake ack, first message
    {"type": "gap", "dropped": 12}        # you missed 12 messages; break the line
    {"type": "ping", "ts": "..."}         # liveness, only while your queue is empty
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone

from fastapi import WebSocket
from fastapi.websockets import WebSocketDisconnect, WebSocketState

log = logging.getLogger(__name__)

# -- thresholds ------------------------------------------------------------- #
# Per-connection outbox depth. 256 messages is ~2 min of one node's stream at
# 2 Hz -- long enough to ride out a browser tab being backgrounded, short enough
# that a truly dead client doesn't hoard memory.
WS_CLIENT_QUEUE_MAX = int(os.getenv("NOVENTIS_WS_CLIENT_QUEUE_MAX", "256"))
# Each connection now costs a task + a queue, so the count needs a ceiling.
MAX_WS_CONNECTIONS = int(os.getenv("NOVENTIS_MAX_WS_CONNECTIONS", "32"))
# Liveness ping. Must stay comfortably below any reverse proxy's read timeout,
# or an idle socket (a quiet node) gets culled and the dashboard goes blank
# until the client's reconnect backoff catches up.
WS_PING_INTERVAL_S = float(os.getenv("NOVENTIS_WS_PING_INTERVAL_S", "20"))

# Close code sent when MAX_WS_CONNECTIONS is reached (RFC 6455 "Try Again Later").
CLOSE_TRY_AGAIN_LATER = 1013


def frame_message(event: dict) -> dict:
    """Event dict -> the JSON-serialisable shape sent to /live clients."""
    return {
        "node_id": event["node_id"],
        "seq_num": event["seq_num"],
        "ts": event["received_at"].isoformat(),
        "values": event["values"],
    }


class _Conn:
    """One client socket: its filter, its outbox, and its send task."""

    __slots__ = ("ws", "node_id", "queue", "task", "sent", "dropped", "pending_gap")

    def __init__(self, ws: WebSocket, node_id: int | None) -> None:
        self.ws = ws
        self.node_id = node_id
        self.queue: asyncio.Queue[str] = asyncio.Queue(maxsize=WS_CLIENT_QUEUE_MAX)
        self.task: asyncio.Task | None = None
        self.sent = 0
        self.dropped = 0
        # Messages dropped since we last told this client about it. Held as a
        # counter rather than queued, because the queue being full is precisely
        # the reason we are dropping -- there is no room to enqueue the notice.
        self.pending_gap = 0

    def offer(self, payload: str) -> None:
        """Enqueue for this client. Never blocks; drops this client's oldest."""
        try:
            self.queue.put_nowait(payload)
        except asyncio.QueueFull:
            try:
                self.queue.get_nowait()
            except asyncio.QueueEmpty:  # pragma: no cover
                pass
            try:
                self.queue.put_nowait(payload)
            except asyncio.QueueFull:   # pragma: no cover
                pass
            self.dropped += 1
            self.pending_gap += 1


class ConnectionManager:
    def __init__(self, bus):
        self._bus = bus
        self._conns: dict[WebSocket, _Conn] = {}
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._ping_task: asyncio.Task | None = None
        self._sent = 0
        self._dropped = 0
        self._rejected = 0

    # -- lifecycle ----------------------------------------------------- #
    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="ws-broadcaster")
        if WS_PING_INTERVAL_S > 0 and (self._ping_task is None or self._ping_task.done()):
            self._ping_task = asyncio.create_task(self._ping_loop(), name="ws-ping")

    async def stop(self) -> None:
        for attr in ("_task", "_ping_task"):
            task = getattr(self, attr)
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                setattr(self, attr, None)
        async with self._lock:
            conns = list(self._conns.values())
            self._conns.clear()
        for conn in conns:
            await self._shutdown_conn(conn)

    # -- per-connection entry point (awaited by the /live endpoint) --- #
    async def serve(self, ws: WebSocket, node_id: int | None) -> None:
        await ws.accept()

        async with self._lock:
            full = len(self._conns) >= MAX_WS_CONNECTIONS
            if not full:
                conn = _Conn(ws, node_id)
                self._conns[ws] = conn
        if full:
            self._rejected += 1
            log.warning(
                "ws connection refused: at MAX_WS_CONNECTIONS (%d)", MAX_WS_CONNECTIONS
            )
            await ws.close(code=CLOSE_TRY_AGAIN_LATER, reason="too many connections")
            return

        # Handshake ack goes through the outbox like everything else, so this
        # socket only ever has one writer.
        conn.offer(json.dumps({"type": "ready", "node_id": node_id}))
        conn.task = asyncio.create_task(self._send_loop(conn), name="ws-send")
        log.info("ws client connected (filter node_id=%s, total=%d)", node_id, len(self._conns))

        try:
            while True:
                # We don't expect inbound messages; this awaits until the client
                # disconnects (or sends something, which we ignore).
                await ws.receive_text()
        except WebSocketDisconnect:
            pass
        except RuntimeError:
            # receive after the socket was already closed by the send loop
            pass
        finally:
            await self._remove(ws)
            log.info("ws client disconnected (total=%d)", len(self._conns))

    # -- per-connection sender (the ONLY writer for its socket) ------- #
    async def _send_loop(self, conn: _Conn) -> None:
        try:
            while True:
                payload = await conn.queue.get()
                # Announce any hole before the message that follows it, so the
                # client can break the series at the right point.
                if conn.pending_gap:
                    missed, conn.pending_gap = conn.pending_gap, 0
                    await conn.ws.send_text(json.dumps({"type": "gap", "dropped": missed}))
                await conn.ws.send_text(payload)
                conn.sent += 1
                self._sent += 1
        except asyncio.CancelledError:
            raise
        except Exception:
            # Broken socket. serve()'s receive_text() sees the disconnect and
            # unregisters; closing here would race with it.
            log.info("ws send failed; dropping connection")

    # -- broadcast loop ---------------------------------------------- #
    async def _run(self) -> None:
        log.info("ws manager subscribed to event bus")
        async for event in self._bus.subscribe("ws-manager"):
            if not event.get("crc_ok") or event.get("control"):
                continue    # control frames (ACKs) are not readings; never charted
            await self._broadcast(event)
            # Yield so the per-connection send tasks can drain. `_broadcast` is
            # pure `put_nowait` and `queue.get()` returns without suspending when
            # the bus queue is backed up, so without this a burst backlog would be
            # fanned out back-to-back and overflow the outboxes of clients that
            # are perfectly healthy -- they just never got scheduled.
            await asyncio.sleep(0)

    async def _broadcast(self, event: dict) -> None:
        node_id = event["node_id"]
        async with self._lock:
            targets = [
                c for c in self._conns.values()
                if c.node_id is None or c.node_id == node_id
            ]
        if not targets:
            return
        payload = json.dumps(frame_message(event))
        for conn in targets:
            before = conn.dropped
            conn.offer(payload)             # never awaits: cannot stall the loop
            if conn.dropped != before:
                self._dropped += 1

    # -- liveness ------------------------------------------------------ #
    async def _ping_loop(self) -> None:
        """Ping only idle clients, so a quiet node doesn't look like a dead socket."""
        while True:
            await asyncio.sleep(WS_PING_INTERVAL_S)
            async with self._lock:
                idle = [c for c in self._conns.values() if c.queue.empty()]
            ping = json.dumps({
                "type": "ping",
                "ts": datetime.now(timezone.utc).isoformat(),
            })
            for conn in idle:
                conn.offer(ping)

    # -- teardown ------------------------------------------------------ #
    async def _shutdown_conn(self, conn: _Conn) -> None:
        """Stop the writer first, then close -- never two writers on one socket."""
        if conn.task is not None:
            conn.task.cancel()
            try:
                await conn.task
            except asyncio.CancelledError:
                pass
            conn.task = None
        if getattr(conn.ws, "client_state", None) != WebSocketState.DISCONNECTED:
            try:
                await conn.ws.close()
            except Exception:
                pass

    async def _remove(self, ws: WebSocket) -> None:
        async with self._lock:
            conn = self._conns.pop(ws, None)
        if conn is not None:
            await self._shutdown_conn(conn)

    # -- introspection (GET /health) ------------------------------- #
    def status(self) -> dict:
        return {
            "running": self._task is not None and not self._task.done(),
            "connections": len(self._conns),
            "max_connections": MAX_WS_CONNECTIONS,
            "client_queue_max": WS_CLIENT_QUEUE_MAX,
            "messages_sent": self._sent,
            "messages_dropped": self._dropped,
            "connections_rejected": self._rejected,
            "clients": [
                {
                    "node_id": c.node_id,
                    "queued": c.queue.qsize(),
                    "sent": c.sent,
                    "dropped": c.dropped,
                }
                for c in self._conns.values()
            ],
        }
