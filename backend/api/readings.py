"""
backend/api/readings.py -- REST: GET /readings?node_id=...

CRC-valid decoded readings for one node, newest first, paginated. Backed by the
(node_id, timestamp) index on the readings table.

Query params:
    node_id   (required)  int
    since     (optional)  ISO-8601 -- return readings with timestamp >= since
    until     (optional)  ISO-8601
    limit     (optional)  int, default 200, max 2000

Response item mirrors the readings columns / docs/protocol-spec.md section 3:
    { "seq_num": ..., "timestamp": ..., "tof_mm": ...,
      "accel_x": ..., "accel_y": ..., "accel_z": ...,
      "gyro_x": ..., "gyro_y": ..., "gyro_z": ... }

TODO:
  - [ ] APIRouter; GET "/readings".
  - [ ] Require node_id; validate limit bounds.
  - [ ] select(Reading).where(node_id==..).order_by(Reading.timestamp.desc()).limit(..)
  - [ ] Pydantic ReadingOut model.
  - [ ] Keyset pagination (before=<timestamp|id>) instead of offset for large sets.
"""

from __future__ import annotations

# from fastapi import APIRouter, Depends, Query
# router = APIRouter(tags=["readings"])
