"""
backend/main.py -- Noventis backend, FastAPI application entrypoint.

Wiring (all lifecycle handled in one lifespan context manager):

  startup:
    1. db.session.init_db()               -- create_all, WAL pragmas
    2. start db.writer.run() task         -- subscribes to the event bus
    3. start ws.manager.run() task        -- subscribes to the event bus
    4. ingest.serial_reader.start()       -- owns the port, publishes to the bus
  shutdown (reverse order):
    serial_reader.stop() -> cancel/await writer + manager tasks -> engine.dispose()

Routes:
    GET  /nodes                      api/nodes.py
    GET  /readings?node_id=...       api/readings.py
    GET  /raw-frames?node_id=...     api/raw_frames.py
    WS   /live?node_id=...           ws/manager.py  (omit node_id for all nodes)

Constraint: nothing in this file (or anything it imports besides
ingest/serial_reader.py) may import pyserial. The reader owns the port; the DB
writer and the WS manager only ever see the in-process event bus.

TODO:
  - [ ] @asynccontextmanager lifespan(app) implementing the sequence above.
  - [ ] app.include_router(...) for the three REST routers.
  - [ ] @app.websocket("/live") delegating to ws.manager.
  - [ ] Config object (env): serial port/baud, DB URL, node timeout, CORS origins.
  - [ ] CORS for the Vite dev server (http://localhost:5173).
"""

from __future__ import annotations

from fastapi import FastAPI

app = FastAPI(title="Noventis", version="0.0.0")


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


# TODO: lifespan, routers, and the /live websocket -- see module docstring.
