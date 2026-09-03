"""
backend/api/debug.py -- REST: GET /debug/frames?limit=&crc_ok=

Window onto the in-memory frame ring buffer (ingest/frame_buffer.py), which
replaced the "every frame in a table" model. Holds the most recent
RAW_FRAME_BUFFER_SIZE candidate frames off the wire, CRC pass and fail; it is
process-lifetime only and empty after a restart.

Returns the same JSON shape as GET /raw-frames -- a bare array, newest first --
except the id is the buffer's own monotonic `seq` (there is no DB row). The
buffer's live size/capacity are reported by GET /health, not here.

  limit    (optional)  1..RAW_FRAME_BUFFER_SIZE, default 200
  crc_ok   (optional)  bool -- e.g. ?crc_ok=false for just the corrupt frames
  node_id  (optional)  int  -- restrict to one node (header must have parsed)
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel

from ..ingest.frame_buffer import RAW_FRAME_BUFFER_SIZE

router = APIRouter(tags=["debug"])


class DebugFrameOut(BaseModel):
    seq: int
    node_id: int | None
    received_at: datetime
    crc_ok: bool
    raw_hex: str


@router.get("/debug/frames", response_model=list[DebugFrameOut])
async def debug_frames(
    request: Request,
    limit: int = Query(200, ge=1, le=RAW_FRAME_BUFFER_SIZE),
    crc_ok: bool | None = None,
    node_id: int | None = None,
) -> list[DebugFrameOut]:
    buf = request.app.state.frame_buffer
    return [
        DebugFrameOut(
            seq=f.seq,
            node_id=f.node_id,
            received_at=f.received_at,
            crc_ok=f.crc_ok,
            raw_hex=f.raw.hex(),
        )
        for f in buf.recent(limit=limit, crc_ok=crc_ok, node_id=node_id)
    ]
