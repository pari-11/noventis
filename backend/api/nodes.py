"""
backend/api/nodes.py -- REST: GET /nodes

Returns every known node with a status COMPUTED at read time (constraint #5):

    status = "online" if (now - last_seen) <= NODE_TIMEOUT else "offline"

Response item:
    { "node_id": 7, "last_seen": "2026-08-30T12:34:56Z", "status": "online" }

TODO:
  - [ ] APIRouter(prefix="", tags=["nodes"]); GET "/nodes".
  - [ ] select(Node).order_by(Node.node_id); map rows -> response model.
  - [ ] NODE_TIMEOUT from env (default e.g. 30s); document it in architecture.md.
  - [ ] Pydantic response model NodeOut.
"""

from __future__ import annotations

# from fastapi import APIRouter, Depends
# router = APIRouter(tags=["nodes"])
