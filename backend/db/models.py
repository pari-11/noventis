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
  - [ ] Add a retention/rollup story for raw_frames (it grows fast).
  - [ ] Confirm node_id domain: protocol NODE_ID is uint8 (1..254).
  - [ ] Store raw int16 fixed-point instead of decoded floats? (lossless replay)
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    Index,
    Integer,
    LargeBinary,
    TypeDecorator,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UtcDateTime(TypeDecorator):
    """Timezone-aware UTC datetimes over SQLite.

    SQLite has no native tz type, so a plain ``DateTime(timezone=True)`` column
    silently hands back *naive* datetimes on read. This decorator stores naive
    UTC and re-attaches ``timezone.utc`` on the way out, so the whole app (and
    the JSON API) always sees aware UTC.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is not None:
            value = value.astimezone(timezone.utc).replace(tzinfo=None)
        return value

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return value.replace(tzinfo=timezone.utc)


class Base(DeclarativeBase):
    pass


class Node(Base):
    """A telemetry node we have heard from at least once."""

    __tablename__ = "nodes"

    node_id: Mapped[int] = mapped_column(Integer, primary_key=True)  # protocol NODE_ID (uint8)
    last_seen: Mapped[datetime] = mapped_column(
        UtcDateTime, default=utcnow, nullable=False
    )
    # status ("online"/"offline") is derived from last_seen at read time -- see
    # api/nodes.py. It is deliberately NOT a column.


class RawFrame(Base):
    """Every candidate frame received -- the forensic log. Never pruned here."""

    __tablename__ = "raw_frames"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    node_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    received_at: Mapped[datetime] = mapped_column(
        UtcDateTime, default=utcnow, nullable=False, index=True
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
        UtcDateTime, default=utcnow, nullable=False
    )

    # Decoded TLV values -- see docs/protocol-spec.md section 3.
    # Flat float columns (not JSON) to mirror base_rx.py's proven schema and keep
    # per-axis filtering/aggregation cheap in SQLite.
    tof_mm: Mapped[int | None] = mapped_column(Integer)          # tag 0x01, millimetres
    accel_x: Mapped[float | None] = mapped_column(Float)         # tag 0x02, m/s^2
    accel_y: Mapped[float | None] = mapped_column(Float)
    accel_z: Mapped[float | None] = mapped_column(Float)
    gyro_x: Mapped[float | None] = mapped_column(Float)          # tag 0x02, rad/s
    gyro_y: Mapped[float | None] = mapped_column(Float)
    gyro_z: Mapped[float | None] = mapped_column(Float)

    __table_args__ = (
        UniqueConstraint("node_id", "seq_num", name="uq_readings_node_seq"),
        Index("ix_readings_node_ts", "node_id", "timestamp"),
    )
