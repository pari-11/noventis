"""
backend/db/session.py -- async engine, session factory, and schema bootstrap.

  - create_async_engine on SQLite via aiosqlite.
  - On every connection: PRAGMA journal_mode=WAL, synchronous=NORMAL,
    foreign_keys=ON  (WAL lets the ingest writer and the API readers work
    concurrently without the writer locking out reads).
  - async_sessionmaker(expire_on_commit=False).
  - init_db(): Base.metadata.create_all (no Alembic yet -- constraint #6).
  - dispose(): called from the FastAPI lifespan on shutdown.

TODO:
  - [ ] Pool / busy-timeout tuning if the writer and API ever contend.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

from sqlalchemy import event, inspect, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from .models import Base

DB_URL = os.getenv("NOVENTIS_DB_URL", "sqlite+aiosqlite:///./noventis.db")


def set_sqlite_pragmas(dbapi_connection, _connection_record) -> None:
    """WAL + sane durability + FK enforcement, applied on every new connection."""
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.close()


def make_engine(url: str = DB_URL) -> AsyncEngine:
    eng = create_async_engine(url, echo=False)
    event.listen(eng.sync_engine, "connect", set_sqlite_pragmas)
    return eng


engine: AsyncEngine = make_engine()
SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


def _retrofit_columns(conn) -> None:
    """Add columns that ``create_all`` cannot add to an already-existing table.

    No Alembic yet (constraint #6). This is a tiny, idempotent stopgap: it only
    ever ADDs a nullable column and back-fills it from data already in the row,
    so it is safe to run on every startup and never touches existing values
    beyond the one new column.
    """
    cols = {c["name"] for c in inspect(conn).get_columns("readings")}
    if "tof_out_of_range" not in cols:
        conn.execute(text("ALTER TABLE readings ADD COLUMN tof_out_of_range BOOLEAN"))
        # Back-fill history so old sentinel rows (~8190 mm) are flagged too.
        conn.execute(text(
            "UPDATE readings SET tof_out_of_range = (tof_mm > 2000) "
            "WHERE tof_mm IS NOT NULL"
        ))

    node_cols = {c["name"] for c in inspect(conn).get_columns("nodes")}
    if "name" not in node_cols:
        # Nullable, no back-fill: a NULL name renders as "Node {id}" on read.
        conn.execute(text("ALTER TABLE nodes ADD COLUMN name VARCHAR(64)"))


async def init_db(target: AsyncEngine | None = None) -> None:
    """Create tables if they do not exist. Called from main.py's lifespan."""
    async with (target or engine).begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_retrofit_columns)


async def dispose() -> None:
    await engine.dispose()


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: yields a session and closes it afterwards."""
    async with SessionLocal() as session:
        yield session
