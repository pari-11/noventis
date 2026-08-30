"""
backend/api/nodes.py -- REST: GET /nodes

Every node we have ever heard from, with its ``last_seen`` and a **computed**
``stale`` flag (no frame within NOVENTIS_STALE_AFTER_S, default 10 s). Node
status is never stored -- it is derived here at read time (constraint #5).
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import Node
from ..db.session import get_session

STALE_AFTER_S = float(os.getenv("NOVENTIS_STALE_AFTER_S", "10"))

router = APIRouter(tags=["nodes"])


class NodeOut(BaseModel):
    node_id: int
    last_seen: datetime
    stale: bool


@router.get("/nodes", response_model=list[NodeOut])
async def list_nodes(session: AsyncSession = Depends(get_session)) -> list[NodeOut]:
    rows = (await session.execute(select(Node).order_by(Node.node_id))).scalars().all()
    now = datetime.now(timezone.utc)
    return [
        NodeOut(
            node_id=n.node_id,
            last_seen=n.last_seen,
            stale=(now - n.last_seen).total_seconds() > STALE_AFTER_S,
        )
        for n in rows
    ]
