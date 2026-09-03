#!/usr/bin/env python3
"""
scripts/sync_protocol.py -- repair drift between the two protocol modules.

``edge/protocol.py`` is the **master copy**: it is what ships to the hardware and
defines the format the deployed firmware speaks. This copies it verbatim over
``backend/ingest/protocol.py`` so the two stay byte-for-byte identical
(CLAUDE.md constraint #2).

Usage (from the repo root):

    python scripts/sync_protocol.py                  # edge/ -> backend/, prompts first
    python scripts/sync_protocol.py --yes            # no prompt
    python scripts/sync_protocol.py --reverse        # backend/ -> edge/, if you edited the mirror
    python scripts/sync_protocol.py --check          # just report, change nothing

Prompts before overwriting and prints the byte counts of both sides first, so an
accidental run in the wrong direction cannot silently discard work. Pair it with
``scripts/check_protocol_sync.py``, which the pre-commit hook runs.

NOTE: this only makes the two files *identical*. It cannot tell you whether the
change itself is correct. A wire-format change must also be reflected in
``docs/protocol-spec.md`` (the source of truth) in the same commit, and
``python edge/protocol.py`` runs the codec's own self-test.
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
from pathlib import Path

EDGE = Path("edge/protocol.py")
BACKEND = Path("backend/ingest/protocol.py")


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def describe(root: Path, rel: Path) -> str:
    p = root / rel
    if not p.is_file():
        return f"{rel.as_posix():<28} MISSING"
    data = p.read_bytes()
    return f"{rel.as_posix():<28} {len(data):>6} bytes  sha256 {hashlib.sha256(data).hexdigest()[:16]}"


def main() -> int:
    ap = argparse.ArgumentParser(description="Keep the two protocol.py modules identical.")
    ap.add_argument("--reverse", action="store_true",
                    help="copy backend/ingest/protocol.py -> edge/protocol.py (use if you edited the mirror)")
    ap.add_argument("--yes", "-y", action="store_true", help="do not prompt")
    ap.add_argument("--check", action="store_true", help="report only, change nothing")
    args = ap.parse_args()

    root = repo_root()
    src_rel, dst_rel = (BACKEND, EDGE) if args.reverse else (EDGE, BACKEND)
    src, dst = root / src_rel, root / dst_rel

    print(describe(root, EDGE))
    print(describe(root, BACKEND))
    print()

    if not src.is_file():
        print(f"source {src_rel.as_posix()} does not exist -- nothing to copy.", file=sys.stderr)
        return 1

    if dst.is_file() and src.read_bytes() == dst.read_bytes():
        print("Already identical -- nothing to do.")
        return 0

    if args.check:
        print("DRIFTED (--check: no changes made).")
        return 1

    print(f"About to overwrite  {dst_rel.as_posix()}")
    print(f"with the copy from  {src_rel.as_posix()}")
    if dst.is_file():
        print(f"\n{dst_rel.as_posix()} will lose any edits it has that the source does not.")

    if not args.yes:
        try:
            reply = input("\nProceed? [y/N] ").strip().lower()
        except EOFError:
            reply = ""
        if reply not in ("y", "yes"):
            print("Aborted -- nothing written.")
            return 1

    shutil.copyfile(src, dst)
    print(f"\nCopied {src_rel.as_posix()} -> {dst_rel.as_posix()}")
    print(describe(root, EDGE))
    print(describe(root, BACKEND))
    print("\nIf this was a wire-format change, update docs/protocol-spec.md in the same commit.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
