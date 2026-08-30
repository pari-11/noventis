"""
backend/api/raw_frames.py -- REST: GET /raw-frames?node_id=&crc_ok=&limit=

Forensic access to the raw_frames log -- every candidate frame received, CRC
pass or fail. Primarily for debugging CRC failures.

  node_id  (optional)  int   -- omit to include frames with an unparseable header (node_id NULL)
  crc_ok   (optional)  bool  -- e.g. ?crc_ok=false to see only corrupt frames
  limit    (optional)  1..2000, default 200
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import RawFrame
from ..db.session import get_session

router = APIRouter(tags=["raw-frames"])


class RawFrameOut(BaseModel):
    id: int
    node_id: int | None
    received_at: datetime
    crc_ok: bool
    raw_hex: str


@router.get("/raw-frames", response_model=list[RawFrameOut])
async def list_raw_frames(
    node_id: int | None = None,
    crc_ok: bool | None = None,
    limit: int = Query(200, ge=1, le=2000),
    session: AsyncSession = Depends(get_session),
) -> list[RawFrameOut]:
    stmt = select(RawFrame)
    if node_id is not None:
        stmt = stmt.where(RawFrame.node_id == node_id)
    if crc_ok is not None:
        stmt = stmt.where(RawFrame.crc_ok.is_(crc_ok))
    stmt = stmt.order_by(RawFrame.received_at.desc(), RawFrame.id.desc()).limit(limit)
    rows = (await session.execute(stmt)).scalars().all()
    return [
        RawFrameOut(
            id=r.id,
            node_id=r.node_id,
            received_at=r.received_at,
            crc_ok=r.crc_ok,
            raw_hex=r.raw.hex(),
        )
        for r in rows
    ]
