"""
backend/api/power.py -- REST: POST /nodes/{node_id}/shutdown

Dashboard "Shut down node" button. Sends the authenticated LoRa shutdown command
(see control/shutdown.py, protocol-spec section 8) and WAITS -- up to about
NOVENTIS_SHUTDOWN_WINDOW_S (default 10 s) -- for the outcome, the same way
``POST /rescan`` waits for the real reconnect result. No new WebSocket message;
``/live`` is untouched.

Body: ``{"confirm": true}`` (stops an accidental request; this API has no auth
yet, see docs/architecture.md "Deferred" -- the real protection is the secret
code the node verifies).

Response: ``{outcome, attempts, message, node_id}`` where outcome is
``acked`` | ``silent`` | ``not_received`` (control/shutdown.py explains each).
409 when remote shutdown is not configured or the radio is disconnected;
404 for an unknown node; 422 without ``confirm``.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from ..control.shutdown import ControlError
from ..db.models import Node
from ..db.session import get_session

router = APIRouter(tags=["power"])


class ShutdownRequest(BaseModel):
    confirm: bool = False


class ShutdownOut(BaseModel):
    node_id: int
    outcome: str
    attempts: int
    message: str


@router.post("/nodes/{node_id}/shutdown", response_model=ShutdownOut)
async def shutdown_node(
    node_id: int,
    body: ShutdownRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> ShutdownOut:
    if not body.confirm:
        raise HTTPException(422, "confirm must be true")
    if await session.get(Node, node_id) is None:
        raise HTTPException(404, f"unknown node {node_id}")
    try:
        result = await request.app.state.control.shutdown(node_id)
    except ControlError as exc:
        raise HTTPException(exc.status, exc.message) from exc
    return ShutdownOut(
        node_id=node_id, outcome=result.outcome,
        attempts=result.attempts, message=result.message,
    )
