"""
backend/db/session.py -- async engine, session factory, and schema bootstrap.

  - create_async_engine on SQLite via aiosqlite.
  - On every connection: PRAGMA journal_mode=WAL, synchronous=NORMAL,
    foreign_keys=ON  (WAL lets the writer and the API readers work concurrently).
  - async_sessionmaker(expire_on_commit=False).
  - init_db(): create_all (no Alembic yet -- constraint #6).

TODO:
  - [ ] Surface pool / timeout tuning if the writer and API contend.
  - [ ] Add a dispose() hook for the FastAPI lifespan shutdown.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from .models import Base

DB_URL = os.getenv("NOVENTIS_DB_URL", "sqlite+aiosqlite:///./noventis.db")

engine = create_async_engine(DB_URL, echo=False, future=True)


@event.listens_for(engine.sync_engine, "connect")
def _sqlite_pragmas(dbapi_connection, _connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def init_db() -> None:
    """Create tables if they do not exist. Called from main.py lifespan."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: yields a session, closes it afterwards."""
    async with SessionLocal() as session:
        yield session
