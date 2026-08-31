"""
backend/api/readings.py -- REST: GET /readings?node_id=&limit=&since=

CRC-valid decoded readings for one node, newest first. Backed by the
(node_id, timestamp) index on the readings table.

  node_id  (required)  int
  limit    (optional)  1..2000, default 200
  since    (optional)  ISO-8601; only readings with timestamp >= since
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import Reading
from ..db.session import get_session

router = APIRouter(tags=["readings"])


class ReadingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    node_id: int
    seq_num: int
    timestamp: datetime
    tof_mm: int | None
    tof_out_of_range: bool | None = None
    accel_x: float | None
    accel_y: float | None
    accel_z: float | None
    gyro_x: float | None
    gyro_y: float | None
    gyro_z: float | None


@router.get("/readings", response_model=list[ReadingOut])
async def list_readings(
    node_id: int,
    limit: int = Query(200, ge=1, le=2000),
    since: datetime | None = None,
    session: AsyncSession = Depends(get_session),
) -> list[Reading]:
    stmt = select(Reading).where(Reading.node_id == node_id)
    if since is not None:
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
        stmt = stmt.where(Reading.timestamp >= since)
    stmt = stmt.order_by(Reading.timestamp.desc(), Reading.id.desc()).limit(limit)
    return list((await session.execute(stmt)).scalars().all())
