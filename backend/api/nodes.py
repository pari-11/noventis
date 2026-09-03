"""
backend/api/nodes.py -- REST: GET /nodes, PATCH /nodes/{node_id}

Every node we have ever heard from, with its ``last_seen``, an operator-assigned
``name`` (falling back to ``"Node {node_id}"`` when unset), and a **computed**
``stale`` flag (no frame within NOVENTIS_STALE_AFTER_S, default 10 s). Node
status is never stored -- it is derived here at read time (constraint #5).

``PATCH /nodes/{node_id}`` sets the display name (JSON body ``{"name": "..."}``)
and returns the updated node.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import Node
from ..db.session import get_session

STALE_AFTER_S = float(os.getenv("NOVENTIS_STALE_AFTER_S", "10"))

router = APIRouter(tags=["nodes"])


class NodeOut(BaseModel):
    node_id: int
    name: str
    last_seen: datetime
    stale: bool


class NodePatch(BaseModel):
    name: str = Field(min_length=1, max_length=64)


def _display_name(node: Node) -> str:
    name = (node.name or "").strip()
    return name or f"Node {node.node_id}"


def _to_out(node: Node, now: datetime) -> NodeOut:
    return NodeOut(
        node_id=node.node_id,
        name=_display_name(node),
        last_seen=node.last_seen,
        stale=(now - node.last_seen).total_seconds() > STALE_AFTER_S,
    )


@router.get("/nodes", response_model=list[NodeOut])
async def list_nodes(session: AsyncSession = Depends(get_session)) -> list[NodeOut]:
    rows = (await session.execute(select(Node).order_by(Node.node_id))).scalars().all()
    now = datetime.now(timezone.utc)
    return [_to_out(n, now) for n in rows]


@router.patch("/nodes/{node_id}", response_model=NodeOut)
async def rename_node(
    node_id: int,
    body: NodePatch,
    session: AsyncSession = Depends(get_session),
) -> NodeOut:
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="name must not be blank")

    node = await session.get(Node, node_id)
    if node is None:
        raise HTTPException(status_code=404, detail=f"node {node_id} not found")

    node.name = name
    await session.commit()
    await session.refresh(node)
    return _to_out(node, datetime.now(timezone.utc))
