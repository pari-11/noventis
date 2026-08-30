"""
backend/db/models.py -- SQLAlchemy 2.0 async ORM models.

Target: SQLite in WAL mode. No Alembic yet (constraint #6) -- schema is created
with `Base.metadata.create_all` from db/session.py:init_db().

Three tables (see docs/architecture.md "Data model"):

  nodes       one row per known node. `status` is COMPUTED on read (api/nodes.py),
              never stored.
  raw_frames  forensic log -- EVERY candidate frame off the wire, CRC pass or
              fail, node_id nullable (header may be unparseable).
  readings    CRC-valid decoded values only. Indexed on (node_id, timestamp).
              UNIQUE (node_id, seq_num) makes re-ingestion idempotent.

TODO:
  - [ ] Decide IMU storage: JSON list columns (below) vs six int columns.
  - [ ] Add a retention/rollup story for raw_frames (it grows fast).
  - [ ] Confirm node_id domain: protocol NODE_ID is uint8 (1..254).
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Index,
    Integer,
    LargeBinary,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Node(Base):
    """A telemetry node we have heard from at least once."""

    __tablename__ = "nodes"

    node_id: Mapped[int] = mapped_column(Integer, primary_key=True)  # protocol NODE_ID (uint8)
    last_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    # status ("online"/"offline") is derived from last_seen at read time -- see
    # api/nodes.py. It is deliberately NOT a column.


class RawFrame(Base):
    """Every candidate frame received -- the forensic log. Never pruned here."""

    __tablename__ = "raw_frames"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    node_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )
    crc_ok: Mapped[bool] = mapped_column(Boolean, nullable=False)
    raw: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)  # exact wire bytes


class Reading(Base):
    """A CRC-valid, decoded measurement set from one frame."""

    __tablename__ = "readings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    node_id: Mapped[int] = mapped_column(Integer, nullable=False)
    seq_num: Mapped[int] = mapped_column(Integer, nullable=False)  # uint16 from header
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    # Decoded TLV values -- keys mirror docs/protocol-spec.md section 3.
    uptime_ms: Mapped[int | None] = mapped_column(BigInteger)
    vbat_mv: Mapped[int | None] = mapped_column(Integer)
    tof_dist_mm: Mapped[int | None] = mapped_column(Integer)
    tof_status: Mapped[int | None] = mapped_column(Integer)
    accel_mg: Mapped[list | None] = mapped_column(JSON)   # [x, y, z] milli-g
    gyro_cdps: Mapped[list | None] = mapped_column(JSON)  # [x, y, z] centi-deg/s
    imu_temp_cc: Mapped[int | None] = mapped_column(Integer)

    __table_args__ = (
        UniqueConstraint("node_id", "seq_num", name="uq_readings_node_seq"),
        Index("ix_readings_node_ts", "node_id", "timestamp"),
    )
