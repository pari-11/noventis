"""
backend/ws/manager.py -- WebSocket fan-out for GET /live.

Subscribes to event_bus.bus independently of db.writer. Never imports pyserial.

Responsibilities:
  - Track connected clients and each client's node_id filter
    (filter is None  ->  client wants ALL nodes).
  - Consume the event bus; for each CRC-valid event, push a JSON message to
    every client whose filter matches event["node_id"].
  - Handle connect / disconnect and slow-client back-pressure (drop message or
    close the socket rather than buffering unboundedly).

Outbound message shape (keys mirror docs/protocol-spec.md section 3):
    {
      "node_id": 7,
      "seq_num": 4211,
      "ts": "2026-08-30T12:34:56.789Z",
      "values": { "tof_dist_mm": 812, "accel_mg": [.,.,.], ... }
    }

TODO:
  - [ ] ConnectionManager: connect(ws, node_id) / disconnect(ws) / broadcast(evt)
  - [ ] One background task: `async with bus.subscribe() as q:` -> broadcast loop.
  - [ ] Started/stopped by main.py lifespan alongside the writer.
  - [ ] Heartbeat / ping to detect dead sockets.
  - [ ] Only forward crc_ok events (raw-frame failures are REST-only).
"""

from __future__ import annotations


class ConnectionManager:
    """Tracks live /live WebSocket clients and their per-node filter."""

    def __init__(self) -> None:
        raise NotImplementedError("ConnectionManager: implement connect/disconnect/broadcast")


manager = ConnectionManager  # TODO: instantiate once implemented


async def run(stop_event) -> None:
    raise NotImplementedError("ws.manager.run(): consume event_bus.bus and fan out to clients")
