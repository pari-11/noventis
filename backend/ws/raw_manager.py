"""
backend/ws/raw_manager.py -- WebSocket fan-out for GET /live/raw (raw packets).

A deliberately SEPARATE sibling of ``ws/manager.py``: its own named bus
subscriber (``"ws-raw"``), its own connection registry, its own per-connection
outboxes. It shares no state with ``/live`` and ``/live`` shares none with it, so
a raw-view client -- however slow, however many -- cannot affect the dashboard's
live stream, and the ``/live`` message contract is untouched.

Unlike ``/live`` this relays EVERY candidate frame (CRC pass and fail), exactly
as received off the wire, as hex. No decoding is involved beyond the node_id /
seq_num the serial reader already extracted from the header.

Same safety rules as ws/manager.py: broadcast is ``put_nowait`` only (never
awaits a socket); each socket has exactly one writer (its send task); a client
that falls behind drops its own oldest messages and is told via a gap message.

Messages::

    {"seq": 1234, "node_id": 1, "seq_num": 4211, "ts": "<ISO-8601>",
     "crc_ok": true, "raw_hex": "aa5501..."}     # data (no "type" key)
    {"type": "ready", "node_id": 1}               # first message
    {"type": "gap", "dropped": 12}                # you missed 12 packets
"""

from __future__ import annotations

import asyncio
import json
import logging
import os

from fastapi import WebSocket
from fastapi.websockets import WebSocketDisconnect, WebSocketState

log = logging.getLogger(__name__)

# Raw view is a debugging tool: few clients, short outbox.
RAW_WS_CLIENT_QUEUE_MAX = int(os.getenv("NOVENTIS_RAW_WS_CLIENT_QUEUE_MAX", "512"))
MAX_RAW_WS_CONNECTIONS = int(os.getenv("NOVENTIS_MAX_RAW_WS_CONNECTIONS", "8"))
CLOSE_TRY_AGAIN_LATER = 1013


class _Conn:
    __slots__ = ("ws", "node_id", "queue", "task", "sent", "dropped", "pending_gap")

    def __init__(self, ws: WebSocket, node_id: int | None) -> None:
        self.ws = ws
        self.node_id = node_id
        self.queue: asyncio.Queue[str] = asyncio.Queue(maxsize=RAW_WS_CLIENT_QUEUE_MAX)
        self.task: asyncio.Task | None = None
        self.sent = 0
        self.dropped = 0
        self.pending_gap = 0

    def offer(self, payload: str) -> None:
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


class RawConnectionManager:
    def __init__(self, bus):
        self._bus = bus
        self._conns: dict[WebSocket, _Conn] = {}
        self._task: asyncio.Task | None = None
        self._seq = 0
        self._sent = 0
        self._rejected = 0

    # -- lifecycle ------------------------------------------------------ #
    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="ws-raw-broadcaster")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        conns = list(self._conns.values())
        self._conns.clear()
        for conn in conns:
            await self._shutdown_conn(conn)

    # -- per-connection entry point (awaited by the /live/raw endpoint) -- #
    async def serve(self, ws: WebSocket, node_id: int | None) -> None:
        await ws.accept()
        if len(self._conns) >= MAX_RAW_WS_CONNECTIONS:
            self._rejected += 1
            await ws.close(code=CLOSE_TRY_AGAIN_LATER, reason="too many connections")
            return

        conn = _Conn(ws, node_id)
        self._conns[ws] = conn
        conn.offer(json.dumps({"type": "ready", "node_id": node_id}))
        conn.task = asyncio.create_task(self._send_loop(conn), name="ws-raw-send")
        try:
            while True:
                await ws.receive_text()     # inbound ignored; awaits disconnect
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            self._conns.pop(ws, None)
            await self._shutdown_conn(conn)

    # -- the ONLY writer for its socket -------------------------------- #
    async def _send_loop(self, conn: _Conn) -> None:
        try:
            while True:
                payload = await conn.queue.get()
                if conn.pending_gap:
                    missed, conn.pending_gap = conn.pending_gap, 0
                    await conn.ws.send_text(json.dumps({"type": "gap", "dropped": missed}))
                await conn.ws.send_text(payload)
                conn.sent += 1
                self._sent += 1
        except asyncio.CancelledError:
            raise
        except Exception:
            log.info("raw ws send failed; dropping connection")

    # -- broadcast ------------------------------------------------------ #
    async def _run(self) -> None:
        log.info("raw ws manager subscribed to event bus")
        async for event in self._bus.subscribe("ws-raw"):
            self._seq += 1
            if not self._conns:
                continue                      # nobody watching: no serialising
            node_id = event.get("node_id")
            targets = [
                c for c in list(self._conns.values())
                if c.node_id is None or c.node_id == node_id
            ]
            if not targets:
                continue
            ts = event["received_at"]
            payload = json.dumps({
                "seq": self._seq,
                "node_id": node_id,
                "seq_num": event.get("seq_num"),
                "ts": ts.isoformat(),
                "crc_ok": bool(event.get("crc_ok")),
                "raw_hex": event["raw"].hex(),
            })
            for conn in targets:
                conn.offer(payload)           # never awaits
            await asyncio.sleep(0)

    async def _shutdown_conn(self, conn: _Conn) -> None:
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

    def status(self) -> dict:
        return {
            "running": self._task is not None and not self._task.done(),
            "connections": len(self._conns),
            "max_connections": MAX_RAW_WS_CONNECTIONS,
            "messages_sent": self._sent,
            "connections_rejected": self._rejected,
        }
