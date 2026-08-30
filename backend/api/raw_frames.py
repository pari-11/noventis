"""
backend/api/raw_frames.py -- REST: GET /raw-frames?node_id=...

Forensic access to the raw_frames log -- every candidate frame received, CRC
pass or fail. node_id filter is OPTIONAL because unparseable headers are stored
with node_id = NULL.

Query params:
    node_id   (optional)  int
    crc_ok    (optional)  bool -- filter to only-valid or only-invalid
    since / until (optional)  ISO-8601
    limit     (optional)  int, default 200, max 2000

Response item:
    { "id": ..., "node_id": 1|null, "received_at": ..., "crc_ok": false,
      "raw_hex": "aa550100 2a ..." }

TODO:
  - [ ] APIRouter; GET "/raw-frames" (note: path uses a hyphen).
  - [ ] Build filters conditionally from the provided params.
  - [ ] Encode `raw` bytes -> hex string in the response.
  - [ ] Newest-first, limit-bounded; keyset pagination for deep scans.
"""

from __future__ import annotations

# from fastapi import APIRouter, Depends, Query
# router = APIRouter(tags=["raw-frames"])
