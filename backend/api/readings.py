"""
backend/api/readings.py -- REST: GET /readings?node_id=&limit=&since=

CRC-valid decoded readings for one node, newest first.

Storage is two-tier (see db/retention.py): the last READINGS_FULL_RES_HOURS live
full-resolution in `readings`; older data is downsampled into per-node
ROLLUP_INTERVAL_MINUTES buckets in `readings_rollup`. This endpoint merges both
so a caller never has to know which table a row came from -- a request whose
`since` reaches past the full-res window transparently picks up bucketed rows for
the older portion.

Bucketed rows are marked `rollup: true` and carry `sample_count`; their
tof/accel/gyro fields are the bucket **average**. (min/max are kept in the table
for diagnostics but not surfaced here.) `seq_num` is 0 and `id` is negative for
bucketed rows -- they have no real frame behind them.

  node_id  (required)  int
  limit    (optional)  1..2000, default 200
  since    (optional)  ISO-8601; only rows at/after this instant
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import Reading, ReadingRollup
from ..db.retention import READINGS_FULL_RES_HOURS
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
    # Set on downsampled rows from readings_rollup; absent/false for full-res rows.
    rollup: bool = False
    sample_count: int | None = None


def _rollup_to_out(r: ReadingRollup) -> ReadingOut:
    return ReadingOut(
        id=-r.id,
        node_id=r.node_id,
        seq_num=0,
        timestamp=r.bucket_start,
        tof_mm=round(r.tof_mm_avg) if r.tof_mm_avg is not None else None,
        tof_out_of_range=None,
        accel_x=r.accel_x_avg,
        accel_y=r.accel_y_avg,
        accel_z=r.accel_z_avg,
        gyro_x=r.gyro_x_avg,
        gyro_y=r.gyro_y_avg,
        gyro_z=r.gyro_z_avg,
        rollup=True,
        sample_count=r.sample_count,
    )


@router.get("/readings", response_model=list[ReadingOut])
async def list_readings(
    node_id: int,
    limit: int = Query(200, ge=1, le=2000),
    since: datetime | None = None,
    session: AsyncSession = Depends(get_session),
) -> list[ReadingOut]:
    if since is not None and since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)

    full_res_boundary = datetime.now(timezone.utc) - timedelta(hours=READINGS_FULL_RES_HOURS)
    want_rollup = since is not None and since < full_res_boundary

    # -- full-resolution tier ------------------------------------------------ #
    stmt = select(Reading).where(Reading.node_id == node_id)
    if since is not None:
        stmt = stmt.where(Reading.timestamp >= since)
    if want_rollup:
        # older-than-boundary rows are served from the rollup tier below; don't
        # double-count the sliver retention hasn't processed yet either.
        stmt = stmt.where(Reading.timestamp >= full_res_boundary)
    stmt = stmt.order_by(Reading.timestamp.desc(), Reading.id.desc()).limit(limit)
    rows: list[ReadingOut] = [
        ReadingOut.model_validate(r) for r in (await session.execute(stmt)).scalars().all()
    ]

    # -- rollup tier (older portion of the requested range) ---------------- #
    if want_rollup and len(rows) < limit:
        rstmt = (
            select(ReadingRollup)
            .where(ReadingRollup.node_id == node_id)
            .where(ReadingRollup.bucket_start >= since)
            .where(ReadingRollup.bucket_start < full_res_boundary)
            .order_by(ReadingRollup.bucket_start.desc())
            .limit(limit - len(rows))
        )
        rows.extend(
            _rollup_to_out(r) for r in (await session.execute(rstmt)).scalars().all()
        )

    return rows
