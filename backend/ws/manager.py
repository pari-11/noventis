"""
backend/ws/manager.py -- WebSocket fan-out for GET /live.

Subscribes to the event bus **independently** of db.writer (its own
``bus.subscribe()`` iterator). Never imports pyserial.

Responsibilities:
  * Track active WebSocket connections, each with an optional ``node_id`` filter
    taken from the ``/live?node_id=`` query param (absent -> receive all nodes).
  * For every CRC-valid frame event, serialise it to JSON and send it only to
    connections whose filter matches ``event["node_id"]`` (or have no filter).
  * Drop connections that error on send; the per-socket ``serve()`` coroutine
    also unregisters on disconnect.

CRC-invalid frames are NOT pushed here -- they are forensic-only and reachable
via GET /raw-frames.

Outbound message (keys per docs/protocol-spec.md section 3)::

    {"node_id": 1, "seq_num": 4211, "ts": "2026-08-30T12:34:56.789+00:00",
     "values": {"tof_mm": 812, "accel_mss": [x,y,z], "gyro_rads": [x,y,z]}}

TODO:
  - [ ] Optional server-side ping if deployments sit behind idle-timeout proxies
        (the ~2 Hz data stream keeps the socket warm in normal operation).
"""

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import WebSocket
from fastapi.websockets import WebSocketDisconnect, WebSocketState

log = logging.getLogger(__name__)


def frame_message(event: dict) -> dict:
    """Event dict -> the JSON-serialisable shape sent to /live clients."""
    return {
        "node_id": event["node_id"],
        "seq_num": event["seq_num"],
        "ts": event["received_at"].isoformat(),
        "values": event["values"],
    }


class ConnectionManager:
    def __init__(self, bus):
        self._bus = bus
        self._conns: dict[WebSocket, int | None] = {}  # ws -> node_id filter (None = all)
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._sent = 0

    # -- lifecycle ----------------------------------------------------- #
    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="ws-broadcaster")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        async with self._lock:
            sockets = list(self._conns)
            self._conns.clear()
        for ws in sockets:
            try:
                await ws.close()
            except Exception:  # pragma: no cover - best effort on shutdown
                pass

    # -- per-connection entry point (awaited by the /live endpoint) --- #
    async def serve(self, ws: WebSocket, node_id: int | None) -> None:
        await ws.accept()
        async with self._lock:
            self._conns[ws] = node_id
        log.info("ws client connected (filter node_id=%s, total=%d)", node_id, len(self._conns))
        # Handshake ack: lets the client confirm it is subscribed before frames flow.
        await ws.send_json({"type": "ready", "node_id": node_id})
        try:
            while True:
                # We don't expect inbound messages; this awaits until the client
                # disconnects (or sends something, which we ignore).
                await ws.receive_text()
        except WebSocketDisconnect:
            pass
        except RuntimeError:
            # receive after the socket was already closed by _broadcast cleanup
            pass
        finally:
            await self._remove(ws)
            log.info("ws client disconnected (total=%d)", len(self._conns))

    # -- broadcast loop ---------------------------------------------- #
    async def _run(self) -> None:
        log.info("ws manager subscribed to event bus")
        async for event in self._bus.subscribe():
            if not event.get("crc_ok"):
                continue
            await self._broadcast(event)

    async def _broadcast(self, event: dict) -> None:
        node_id = event["node_id"]
        async with self._lock:
            targets = [ws for ws, flt in self._conns.items() if flt is None or flt == node_id]
        if not targets:
            return
        payload = json.dumps(frame_message(event))
        dead: list[WebSocket] = []
        for ws in targets:
            try:
                await ws.send_text(payload)
                self._sent += 1
            except Exception:
                dead.append(ws)
        for ws in dead:
            await self._remove(ws)

    async def _remove(self, ws: WebSocket) -> None:
        async with self._lock:
            self._conns.pop(ws, None)
        if getattr(ws, "client_state", None) != WebSocketState.DISCONNECTED:
            try:
                await ws.close()
            except Exception:
                pass

    # -- introspection (GET /health) ------------------------------- #
    def status(self) -> dict:
        return {
            "running": self._task is not None and not self._task.done(),
            "connections": len(self._conns),
            "messages_sent": self._sent,
        }
