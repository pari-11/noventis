#!/usr/bin/env python3
"""
scripts/verify_archive.py -- reconcile the archive against the database.

The archive is meant to become the system of record, and eventually retention
will delete SQLite rows on the strength of its manifests. Before trusting it that
far, you want evidence from your own data that it is not quietly missing
readings. That is what this does: it reads both copies and compares them.

Two checks, and they answer different questions:

  1. MANIFEST INTEGRITY -- for every sealed hour, does the stored segment still
     hash to what its manifest claims, and does it hold the number of rows the
     manifest claims? Catches bit-rot and a truncated or half-written segment.

  2. RECONCILIATION -- for the window where both copies still exist, is every
     reading in `readings` also in the archive?

DIRECTION MATTERS. The two kinds of mismatch are not symmetric:

  * **In SQLite but NOT in the archive -> a real failure.** The backend received
    the reading and the database stored it, so the archive should have it too.
    This is the number that must be zero.

  * **In the archive but NOT in SQLite -> expected, not an error.** Two ordinary
    causes: a node reboot makes `db/writer.py` clear that node's prior `readings`
    (the archive is append-only and keeps them), and retention eventually rolls
    old rows away. The archive holding *more* is the entire point.

WHAT THIS CANNOT TELL YOU
    Only the overlap window can be checked -- once a reading has aged out of
    `readings` there is nothing left to compare against, so verification has a
    shelf life. It proves the pipeline was sound over the window you checked, not
    that it is sound now, and it can never detect a frame lost over the air
    (protocol-spec section 6): the backend never saw it, so neither copy has it.

Usage (from the repo root):

    python scripts/verify_archive.py                  # last 24 h, all nodes
    python scripts/verify_archive.py --hours 168
    python scripts/verify_archive.py --node-id 1 --verbose
    python scripts/verify_archive.py --manifests-only
    python scripts/verify_archive.py --db path/to/noventis.db --archive-dir ./archive

Exit code 0 when the archive is complete over the window, 1 when readings are
missing from it or a manifest fails its own checksum.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

VALUE_COLS = ("tof_mm", "accel_x", "accel_y", "accel_z", "gyro_x", "gyro_y", "gyro_z")


# -- input -------------------------------------------------------------- #
def db_path_from_env(explicit: str | None) -> str:
    if explicit:
        return explicit
    url = os.getenv("NOVENTIS_DB_URL", "sqlite+aiosqlite:///./noventis.db")
    return url.split("///", 1)[1] if "///" in url else "./noventis.db"


def parse_ts(value: str) -> datetime:
    """Both stores keep UTC; only the spelling differs (ISO+offset vs SQLite text)."""
    v = value.strip().replace(" ", "T")
    dt = datetime.fromisoformat(v)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def iter_archive_rows(archive_dir: Path):
    """Every archived reading: sealed .gz segments plus the still-open spool."""
    data = archive_dir / "data"
    spool = archive_dir / "spool"
    for path in sorted(data.rglob("*.ndjson.gz")):
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            yield from _rows(fh, path)
    for path in sorted(spool.glob("node=*/*.ndjson")):
        with open(path, "r", encoding="utf-8") as fh:
            yield from _rows(fh, path)


def _rows(fh, path: Path):
    for line in fh:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue                      # torn final line of a crashed write
        if "_gap" in obj or "_close" in obj:
            continue
        obj["_source"] = path.name
        yield obj


# -- check 1: manifests ------------------------------------------------- #
def check_manifests(archive_dir: Path, verbose: bool) -> tuple[int, int, list[str]]:
    """Verify every hour's manifest against what is actually stored.

    Schema 2 manifests hold a ``parts`` list rather than one segment: an hour can
    be sealed more than once (a restart force-closes whatever hour is open even
    though it has not finished), and each seal is its own numbered part rather
    than overwriting the last one -- see writer.py's ``_compact`` docstring for
    the bug this replaced (verify_archive.py is what surfaced it: readings the
    database had were missing from the archive after a restart clobbered them).
    Schema 1 (a single top-level segment) is still read, for archives sealed
    before this fix.
    """
    data = archive_dir / "data"
    problems: list[str] = []
    checked = incomplete = 0

    for mpath in sorted(data.rglob("_manifest.json")):
        checked += 1
        rel = mpath.parent.relative_to(data)
        try:
            m = json.loads(mpath.read_text(encoding="utf-8"))
        except Exception as e:
            problems.append(f"{rel}: manifest unreadable ({e})")
            continue

        if m.get("schema") == 2:
            parts = m.get("parts", [])
        else:
            # schema 1: the whole manifest describes its one implicit part.
            parts = [{
                "key": m.get("data_key", ""), "rows": m.get("rows_written", 0),
                "sha256": m.get("sha256"), "sealed": m.get("sealed", True),
                "truncated": m.get("truncated", False),
            }]

        if not m.get("complete", False):
            incomplete += 1
            why = []
            if m.get("drops_observed"):
                why.append(f"{m['drops_observed']} drop(s)")
            if not m.get("sealed", True):
                why.append("unclean shutdown")
            if m.get("truncated"):
                why.append("truncated write")
            if len(parts) > 1:
                why.append(f"{len(parts)} parts")
            print(f"  INCOMPLETE  {rel}  ({', '.join(why) or 'unknown'}) "
                  f"-- {m.get('rows_written', '?')} rows, never certifiable")
        elif len(parts) > 1 and verbose:
            print(f"  {rel}: {len(parts)} parts (hour was sealed across a restart)")

        total_actual = 0
        part_problem = False
        for i, part in enumerate(parts):
            seg = mpath.parent / Path(part.get("key", "")).name
            if not seg.is_file():
                problems.append(f"{rel} part {i}: segment missing ({seg.name})")
                part_problem = True
                continue
            digest = hashlib.sha256(seg.read_bytes()).hexdigest()
            if digest != part.get("sha256"):
                problems.append(f"{rel} part {i}: sha256 MISMATCH -- segment altered or corrupt")
                part_problem = True
                continue
            with gzip.open(seg, "rt", encoding="utf-8") as fh:
                actual = sum(1 for _ in _rows(fh, seg))
            if actual != part.get("rows"):
                problems.append(
                    f"{rel} part {i}: row count {actual} != manifest {part.get('rows')}"
                )
                part_problem = True
            else:
                total_actual += actual

        if not part_problem:
            claimed = m.get("rows_written", sum(p.get("rows", 0) for p in parts))
            if total_actual != claimed:
                problems.append(
                    f"{rel}: aggregate row count {total_actual} != manifest {claimed}"
                )
            elif verbose:
                print(f"  ok          {rel}  {total_actual} rows across {len(parts)} "
                      f"part(s), sha256 verified")

    return checked, incomplete, problems


# -- check 2: reconciliation -------------------------------------------- #
def reconcile(db_file: str, archive_dir: Path, since: datetime, until: datetime,
              node_id: int | None, verbose: bool) -> tuple[int, int, int, list[str]]:
    # Load the archive first: its earliest row bounds what can meaningfully be
    # compared. Readings the database captured BEFORE the archive existed are
    # legitimately absent from it -- counting those as failures would report a
    # permanent, unfixable FAIL and make the tool useless.
    arch: dict[tuple[int, int], dict] = {}
    archive_start: datetime | None = None
    for row in iter_archive_rows(archive_dir):
        if node_id is not None and row.get("node_id") != node_id:
            continue
        try:
            ts = parse_ts(row["ts"])
        except (KeyError, ValueError):
            continue
        if archive_start is None or ts < archive_start:
            archive_start = ts
        if ts < since or ts > until:
            continue
        arch[(row["node_id"], row["seq_num"])] = row

    if archive_start is None:
        print("  the archive holds no rows for this window -- nothing to reconcile")
        return 0, 0, 0, []

    effective = since
    if archive_start > since:
        effective = archive_start
        print(f"  window narrowed to the overlap: the archive starts at "
              f"{archive_start.isoformat(timespec='seconds')}")
        print(f"  (earlier readings predate the archive and are not expected in it)")

    con = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    sql = "SELECT node_id, seq_num, timestamp, " + ", ".join(VALUE_COLS) + " FROM readings"
    where, args = [], []
    if node_id is not None:
        where.append("node_id = ?")
        args.append(node_id)
    if where:
        sql += " WHERE " + " AND ".join(where)

    db_rows: dict[tuple[int, int], sqlite3.Row] = {}
    for r in con.execute(sql, args):
        ts = parse_ts(r["timestamp"])
        if ts < effective or ts > until:
            continue
        db_rows[(r["node_id"], r["seq_num"])] = r
    con.close()

    missing = sorted(set(db_rows) - set(arch))
    extra = sorted(set(arch) - set(db_rows))

    mismatches: list[str] = []
    for key in sorted(set(db_rows) & set(arch)):
        d, a = db_rows[key], arch[key]
        for col in VALUE_COLS:
            dv, av = d[col], a.get(col)
            if dv is None and av is None:
                continue
            if dv is None or av is None or abs(float(dv) - float(av)) > 1e-6:
                mismatches.append(f"node {key[0]} seq {key[1]}: {col} db={dv!r} archive={av!r}")
                break

    if verbose and extra:
        print(f"  (archive holds {len(extra)} reading(s) the database no longer has -- "
              f"expected after a node reboot or retention)")
    for key in missing[:20]:
        r = db_rows[key]
        print(f"  MISSING     node {key[0]} seq {key[1]} @ {r['timestamp']} "
              f"-- in the database, absent from the archive")
    if len(missing) > 20:
        print(f"  ... and {len(missing) - 20} more")
    for m in mismatches[:20]:
        print(f"  MISMATCH    {m}")

    return len(db_rows), len(missing), len(extra), mismatches


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Reconcile the archive against the database.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Exit 0 = archive complete over the window; 1 = readings missing "
               "or a manifest failed verification.",
    )
    ap.add_argument("--db", help="path to noventis.db (default: $NOVENTIS_DB_URL or ./noventis.db)")
    ap.add_argument("--archive-dir", default=os.getenv("NOVENTIS_ARCHIVE_DIR", "./archive"))
    ap.add_argument("--hours", type=float, default=24.0,
                    help="how far back to reconcile (default 24; the database only "
                         "keeps READINGS_FULL_RES_HOURS, so beyond that there is "
                         "nothing left to compare against)")
    ap.add_argument("--node-id", type=int, help="restrict to one node")
    ap.add_argument("--grace-seconds", type=float, default=30.0,
                    help="ignore readings newer than this (default 30). The archive "
                         "buffers writes and fsyncs on an interval, so the newest "
                         "readings are legitimately not on disk yet; without a "
                         "settling window every run against a live backend would "
                         "report a false failure")
    ap.add_argument("--manifests-only", action="store_true", help="skip reconciliation")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args()

    archive_dir = Path(args.archive_dir)
    db_file = db_path_from_env(args.db)

    if not archive_dir.is_dir():
        print(f"archive directory not found: {archive_dir}", file=sys.stderr)
        return 1

    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=args.hours)
    until = now - timedelta(seconds=args.grace_seconds)
    print(f"archive : {archive_dir.resolve()}")
    print(f"database: {Path(db_file).resolve()}")
    print(f"window  : last {args.hours:g} h (since {since.isoformat(timespec='seconds')})")
    print(f"settling: ignoring the last {args.grace_seconds:g} s "
          f"(not yet fsynced to the spool)")
    if args.node_id is not None:
        print(f"node    : {args.node_id}")
    print()

    print("Manifest integrity")
    checked, incomplete, problems = check_manifests(archive_dir, args.verbose)
    for p in problems:
        print(f"  PROBLEM     {p}")
    print(f"  {checked} sealed hour(s) checked, {incomplete} incomplete, "
          f"{len(problems)} problem(s)")
    print()

    missing = extra = total = 0
    mismatches: list[str] = []
    if not args.manifests_only:
        print("Reconciliation (database -> archive)")
        total, missing, extra, mismatches = reconcile(
            db_file, archive_dir, since, until, args.node_id, args.verbose
        )
        print(f"  {total} reading(s) in the database over the window")
        print(f"  {missing} missing from the archive       <- must be 0")
        print(f"  {len(mismatches)} value mismatch(es)             <- must be 0")
        print(f"  {extra} archive-only (expected: reboots, retention)")
        print()

    ok = not problems and missing == 0 and not mismatches
    if ok:
        print("PASS -- every reading the database has is in the archive, "
              "and every sealed segment verifies.")
        if incomplete:
            print(f"NOTE -- {incomplete} hour(s) are marked incomplete and can never be "
                  "certified. That is the taint working as designed, not a new fault.")
    else:
        print("FAIL -- see above. Do NOT gate retention on these manifests.")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
