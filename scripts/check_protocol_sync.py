#!/usr/bin/env python3
"""
scripts/check_protocol_sync.py -- enforce constraint #2: the two protocol modules
must stay byte-for-byte identical.

``edge/protocol.py`` encodes what the node transmits; ``backend/ingest/protocol.py``
decodes what the backend receives. CLAUDE.md calls this the project's single point
of failure, and until now nothing enforced it -- the rule lived only in a human's
memory.

Drift fails *late and misleadingly*: the node keeps transmitting happily and the
backend rejects every frame as corrupt, so it presents as a radio or wiring fault,
not a code change. This check turns that into an immediate, local failure.

Usage (from the repo root):

    python scripts/check_protocol_sync.py          # exit 0 identical, 1 drifted
    python scripts/check_protocol_sync.py --quiet  # no output on success

Wired into .githooks/pre-commit, so a commit that drifts them is refused. Install
the hooks once per clone:

    git config core.hooksPath .githooks

Run ``python scripts/sync_protocol.py`` to repair drift (edge/ is the master copy).

Deliberately dependency-free and standalone (not a pytest case): there is no test
runner or CI in this project yet, and this must run from a git hook regardless.
When CI arrives, call this same script from it.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

# edge/ is the master copy: it is what ships to the hardware.
MASTER = Path("edge/protocol.py")
MIRROR = Path("backend/ingest/protocol.py")


def repo_root() -> Path:
    """The repo root, so the check works from any working directory."""
    return Path(__file__).resolve().parent.parent


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--quiet", action="store_true", help="print nothing on success")
    args = ap.parse_args()

    root = repo_root()
    master, mirror = root / MASTER, root / MIRROR

    missing = [p for p in (master, mirror) if not p.is_file()]
    if missing:
        for p in missing:
            print(f"protocol sync: MISSING {p.relative_to(root).as_posix()}", file=sys.stderr)
        return 1

    a, b = master.read_bytes(), mirror.read_bytes()
    if a == b:
        if not args.quiet:
            digest = hashlib.sha256(a).hexdigest()
            print(f"protocol sync: OK -- both files identical ({len(a)} bytes, sha256 {digest[:12]})")
        return 0

    print("protocol sync: FAILED -- the two protocol modules have drifted.", file=sys.stderr)
    print("", file=sys.stderr)
    for path, data in ((MASTER, a), (MIRROR, b)):
        print(
            f"  {path.as_posix():<28} {len(data):>6} bytes  sha256 {hashlib.sha256(data).hexdigest()[:16]}",
            file=sys.stderr,
        )
    print("", file=sys.stderr)
    print("They must stay byte-for-byte identical (CLAUDE.md constraint #2).", file=sys.stderr)
    print("If the change belongs in the wire format, update docs/protocol-spec.md too.", file=sys.stderr)
    print("", file=sys.stderr)
    print(f"  python scripts/sync_protocol.py     # copy {MASTER.as_posix()} -> {MIRROR.as_posix()}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
