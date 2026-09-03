#!/usr/bin/env python3
"""
scripts/backup_db.py -- consistent snapshot of noventis.db while the backend runs.

The whole dataset is a single SQLite file. This makes a point-in-time copy that
is safe to take with the backend live, using SQLite's own mechanisms
(``VACUUM INTO``, or the online backup API as a fallback) -- **never** a raw
file copy: noventis.db runs in WAL mode, so `cp noventis.db backup.db` can catch
the main file and the -wal out of sync and produce a corrupt or stale snapshot.

``VACUUM INTO`` runs inside an ordinary read transaction (no exclusive lock), so
concurrent ingestion is not blocked, and the output is also defragmented and
checkpointed (plain rollback-journal mode -- the app re-enables WAL on first
open).

Usage (from the repo root):

    python scripts/backup_db.py                       # -> ./backups/noventis-YYYYMMDD-HHMMSSZ.db
    python scripts/backup_db.py --out-dir /mnt/backup
    python scripts/backup_db.py --out /path/to/snapshot.db
    python scripts/backup_db.py --db path/to/noventis.db

The source DB defaults to $NOVENTIS_DB_URL (parsed) or ./noventis.db.

RECOMMENDED CADENCE: at least daily (cron / Windows Task Scheduler). See the
retention warning in README.md -- full-resolution readings older than
READINGS_FULL_RES_HOURS (default 168 h / 7 days) are rolled up to 1-minute
averages and the originals deleted, permanently. If the raw 2 Hz data matters,
a backup MUST be taken within that window; nothing can reconstruct it afterwards.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone


def db_path_from_env(explicit: str | None) -> str:
    if explicit:
        return explicit
    url = os.getenv("NOVENTIS_DB_URL", "sqlite+aiosqlite:///./noventis.db")
    if "sqlite" not in url:
        sys.exit(f"NOVENTIS_DB_URL is not a SQLite URL: {url!r} -- pass --db explicitly")
    after = url.split(":///", 1)[1] if ":///" in url else url.rsplit("/", 1)[-1]
    return after or "./noventis.db"


def make_snapshot(src: str, dst: str) -> None:
    """VACUUM INTO if available (>= 3.27), else the online backup API."""
    conn = sqlite3.connect(src, timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")
    try:
        ver = tuple(int(x) for x in sqlite3.sqlite_version.split("."))
        if ver >= (3, 27, 0):
            conn.execute("VACUUM INTO ?", (dst,))
        else:  # pragma: no cover - very old SQLite
            with sqlite3.connect(dst) as out:
                conn.backup(out)
    finally:
        conn.close()


def verify(path: str) -> tuple[bool, dict[str, int]]:
    conn = sqlite3.connect(path, timeout=30)
    try:
        ok = conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        counts = {}
        for tbl in ("nodes", "raw_frames", "readings", "readings_rollup"):
            try:
                counts[tbl] = conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
            except sqlite3.OperationalError:
                counts[tbl] = -1  # table absent in this snapshot
        return ok, counts
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", help="source noventis.db (default: from NOVENTIS_DB_URL or ./noventis.db)")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--out", help="exact output file path")
    g.add_argument("--out-dir", default="backups", help="directory for the timestamped snapshot (default: ./backups)")
    args = ap.parse_args()

    src = db_path_from_env(args.db)
    if not os.path.exists(src):
        sys.exit(f"source database not found: {src}")

    if args.out:
        dst = args.out
        os.makedirs(os.path.dirname(os.path.abspath(dst)), exist_ok=True)
    else:
        os.makedirs(args.out_dir, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%SZ")
        dst = os.path.join(args.out_dir, f"noventis-{stamp}.db")
    if os.path.exists(dst):
        sys.exit(f"refusing to overwrite existing file: {dst}")

    print(f"source : {src}  ({os.path.getsize(src):,} bytes)")
    print(f"target : {dst}")
    t0 = time.monotonic()
    make_snapshot(src, dst)
    dt = time.monotonic() - t0

    ok, counts = verify(dst)
    print(f"\nwrote {os.path.getsize(dst):,} bytes in {dt:.2f}s")
    print(f"quick_check: {'ok' if ok else 'FAILED'}")
    print("row counts : " + ", ".join(f"{k}={v:,}" for k, v in counts.items()))
    if not ok:
        print("\nsnapshot failed integrity check -- do NOT rely on it.", file=sys.stderr)
        return 2
    print("\nsnapshot is valid and openable.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
