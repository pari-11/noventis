#!/usr/bin/env python3
"""
scripts/migrate_purge_legacy_frames.py -- one-off, operator-run cleanup.

Before the raw-frame storage model changed, backend/db/writer.py wrote EVERY
candidate frame (CRC pass and fail) to the `raw_frames` table. Now it writes only
CRC-*failed* frames; CRC-valid frames live for ~40 min in an in-memory ring
buffer and are never persisted (see backend/ingest/frame_buffer.py).

A database created under the old model therefore still carries a large block of
CRC-valid `raw_frames` rows that nothing reads any more. This script deletes
them -- `DELETE FROM raw_frames WHERE crc_ok = 1` -- after showing the row count
and asking for confirmation.

The backend deliberately does NOT do this at startup: restoring an older backup
must never trigger a silent purge. Run this by hand, once, when you have decided
those rows are disposable.

Usage (from the repo root):

    python scripts/migrate_purge_legacy_frames.py            # interactive
    python scripts/migrate_purge_legacy_frames.py --dry-run  # count only, no delete
    python scripts/migrate_purge_legacy_frames.py --yes      # skip the prompt (for scripts)
    python scripts/migrate_purge_legacy_frames.py --db path/to/noventis.db

The DB defaults to $NOVENTIS_DB_URL (parsed) or ./noventis.db. Safe to run while
the backend is up (a plain DELETE in WAL mode); the file only shrinks after a
VACUUM -- use scripts/backup_db.py (VACUUM INTO) or a maintenance-window
`sqlite3 noventis.db 'VACUUM;'` for that.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import datetime, timezone


def db_path_from_env(explicit: str | None) -> str:
    if explicit:
        return explicit
    url = os.getenv("NOVENTIS_DB_URL", "sqlite+aiosqlite:///./noventis.db")
    # sqlite+aiosqlite:///./noventis.db  ->  ./noventis.db
    # sqlite:////abs/path/noventis.db    ->  /abs/path/noventis.db
    if "sqlite" not in url:
        sys.exit(f"NOVENTIS_DB_URL is not a SQLite URL: {url!r} -- pass --db explicitly")
    after = url.split(":///", 1)[1] if ":///" in url else url.rsplit("/", 1)[-1]
    return after or "./noventis.db"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", help="path to noventis.db (default: from NOVENTIS_DB_URL or ./noventis.db)")
    ap.add_argument("--dry-run", action="store_true", help="print the count and exit without deleting")
    ap.add_argument("--yes", action="store_true", help="do not prompt (non-interactive)")
    args = ap.parse_args()

    path = db_path_from_env(args.db)
    if not os.path.exists(path):
        sys.exit(f"database not found: {path}")

    size_before = os.path.getsize(path)
    conn = sqlite3.connect(path, timeout=15)
    conn.execute("PRAGMA busy_timeout=15000")
    try:
        total = conn.execute("SELECT COUNT(*) FROM raw_frames").fetchone()[0]
        legacy = conn.execute("SELECT COUNT(*) FROM raw_frames WHERE crc_ok = 1").fetchone()[0]
        keep = total - legacy
        span = conn.execute(
            "SELECT MIN(received_at), MAX(received_at) FROM raw_frames WHERE crc_ok = 1"
        ).fetchone()

        print(f"database        : {path}  ({size_before:,} bytes)")
        print(f"raw_frames total: {total:,}")
        print(f"  CRC-valid (to delete) : {legacy:,}")
        if legacy:
            print(f"  spanning              : {span[0]}  ..  {span[1]}  (UTC)")
        print(f"  CRC-failed (kept)     : {keep:,}")

        if legacy == 0:
            print("\nNothing to purge -- raw_frames is already CRC-failures-only.")
            return 0
        if args.dry_run:
            print("\n--dry-run: no changes made.")
            return 0

        if not args.yes:
            print(
                f"\nThis will permanently delete {legacy:,} CRC-valid raw_frames rows.\n"
                "CRC-failed frames are untouched. This cannot be undone (restore from a backup)."
            )
            resp = input("Type 'yes' to proceed: ").strip().lower()
            if resp != "yes":
                print("Aborted -- no changes made.")
                return 1

        with conn:  # transaction
            deleted = conn.execute("DELETE FROM raw_frames WHERE crc_ok = 1").rowcount
        # reclaim trailing free pages if the DB is in incremental auto_vacuum mode
        # (a no-op otherwise); a full VACUUM is left to a maintenance window.
        if conn.execute("PRAGMA auto_vacuum").fetchone()[0] == 2:
            conn.execute("PRAGMA incremental_vacuum")
            conn.commit()
    finally:
        conn.close()

    size_after = os.path.getsize(path)
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    print(f"\n[{stamp}] deleted {deleted:,} rows.")
    print(f"file size: {size_before:,} -> {size_after:,} bytes")
    if size_after >= size_before:
        print("(file not yet smaller -- pages are freed for reuse; run VACUUM / "
              "scripts/backup_db.py to shrink the file on disk.)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
