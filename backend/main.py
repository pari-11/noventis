"""
backend/main.py -- Noventis backend, FastAPI application entrypoint.

Run from the repo root:

    uvicorn backend.main:app --reload --port 8000

Lifespan startup, in order:
  1. init_db()                       -- create_all + WAL pragmas
  2. DBWriter.start()                -- subscribes to the event bus
  3. ConnectionManager.start()       -- subscribes to the event bus (independently)
  4. SerialReader.start()            -- owns the CP2102 port in a background thread;
                                        publishes frames onto the bus via a
                                        loop-safe bridge

Shutdown tears them down in reverse and disposes the DB engine.

Routes:
  GET  /health                     -- LoRa port connected? how many nodes seen?
  GET  /session                    -- when this backend run started (SESSION_START_TS)
  GET  /nodes                      -- api/nodes.py
  GET  /readings?node_id=...       -- api/readings.py
  GET  /raw-frames?node_id=...     -- api/raw_frames.py
  WS   /live?node_id=...           -- ws/manager.py (omit node_id for all nodes)

Only ingest/serial_reader.py touches pyserial; every other component sees the
in-process event bus.
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import func, select

from .api import nodes, raw_frames, readings
from .db.models import Node
from .db.session import SessionLocal, dispose, init_db
from .db.writer import DBWriter
from .ingest.event_bus import bus
from .ingest.serial_reader import (
    SerialReader,
    loop_safe_publisher,
    node_port_map_from_env,
)
from .ws.manager import ConnectionManager

logging.basicConfig(
    level=os.getenv("NOVENTIS_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("noventis")

# When this backend process started. Set once, at import/startup; a restart
# resets it -- intentional, no persistence. The frontend reads this via
# GET /session and passes it as `since=` to bound "current session" views.
SESSION_START_TS = datetime.now(timezone.utc)

CORS_ORIGINS = [
    o.strip()
    for o in os.getenv("NOVENTIS_CORS_ORIGINS", "http://localhost:5173").split(",")
    if o.strip()
]


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()

    writer = DBWriter(bus)
    ws_manager = ConnectionManager(bus)
    writer.start()
    ws_manager.start()
    await asyncio.sleep(0)  # let both tasks reach bus.subscribe() before frames flow

    reader = SerialReader(
        publish=loop_safe_publisher(asyncio.get_running_loop(), bus.publish),
        node_port_map=node_port_map_from_env(),
    )
    reader.start()

    app.state.reader = reader
    app.state.writer = writer
    app.state.ws_manager = ws_manager
    log.info("noventis backend started (CORS origins: %s)", CORS_ORIGINS or "none")

    try:
        yield
    finally:
        await asyncio.to_thread(reader.stop)
        await ws_manager.stop()
        await writer.stop()
        await dispose()
        log.info("noventis backend stopped")


app = FastAPI(title="Noventis", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_methods=["GET", "POST", "PATCH"],
    allow_headers=["*"],
)

app.include_router(nodes.router)
app.include_router(readings.router)
app.include_router(raw_frames.router)


@app.get("/health", tags=["meta"])
async def health() -> dict:
    reader = app.state.reader
    rstat = reader.status()
    async with SessionLocal() as session:
        nodes_seen = (await session.execute(select(func.count()).select_from(Node))).scalar_one()
    return {
        "status": "ok",
        "session_start": SESSION_START_TS.isoformat(),
        "lora": {
            "connected": rstat["connected"],
            "port": rstat["port"],
            "baud": rstat["baud"],
            "bytes_read": rstat["bytes_read"],
            "frames_ok": rstat["frames_ok"],
            "frames_bad": rstat["frames_bad"],
            "last_error": rstat["last_error"],
        },
        "nodes_seen": nodes_seen,
        "writer": app.state.writer.status(),
        "ws": app.state.ws_manager.status(),
        "bus": bus.status(),
    }


@app.get("/session", tags=["meta"])
async def session_info() -> dict:
    """When the current backend run began. Resets on restart (no persistence)."""
    return {"session_start": SESSION_START_TS.isoformat()}


# How long POST /rescan waits for the reader thread to (re)connect before it
# reports failure. The thread reacts within a read timeout (~0.2 s) and a present
# adapter reopens well inside this window.
RESCAN_WAIT_S = float(os.getenv("NOVENTIS_RESCAN_WAIT_S", "4"))


@app.post("/rescan", tags=["meta"])
async def rescan_ports() -> dict:
    """Re-run CP2102 serial auto-detection now and report the real outcome.

    Forces the background serial reader to drop any open port and re-scan (the
    same discovery it does on startup / when the adapter drops), then waits a
    bounded window for it to reconnect. The dashboard's "Rescan for nodes"
    button drives its idle -> reconnecting -> success | failure states off this
    response, not a timer.
    """
    reader = app.state.reader
    reader.request_rescan()

    loop = asyncio.get_running_loop()
    # let the thread actually drop the current port before we start checking, so
    # the happy path reflects a genuine reconnect rather than the stale state
    await asyncio.sleep(0.5)
    deadline = loop.time() + RESCAN_WAIT_S
    while loop.time() < deadline:
        st = reader.status()
        if st["connected"]:
            return {"ok": True, "connected": True, "port": st["port"], "last_error": None}
        await asyncio.sleep(0.25)

    st = reader.status()
    return {
        "ok": False,
        "connected": st["connected"],
        "port": st["port"],
        "last_error": st["last_error"],
    }


@app.websocket("/live")
async def live(websocket: WebSocket, node_id: int | None = None) -> None:
    await app.state.ws_manager.serve(websocket, node_id)
