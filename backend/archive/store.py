"""
backend/archive/store.py -- where finished archive segments go.

**This module is the seam.** The archive writer never knows what storage is; it
hands a finished file to a ``Store`` and asks for it back by key. Today that is
``LocalStore`` (a directory on this disk). Connecting a data lake later means one
more implementation of the same three methods -- an ``S3Store`` of roughly thirty
lines using boto3 against S3 / Cloudflare R2 -- plus a config switch. Nothing in
``writer.py``, ``retention.py``, the database, the API or the frontend changes.

Keys are Hive-style partition paths, so an engine that reads the archive later
(DuckDB, Athena, BigQuery external tables) can prune on the two fields the API
actually filters by, without listing every object::

    readings/node_id=1/date=2026-09-03/hour=05/part-0000.ndjson.gz
    readings/node_id=1/date=2026-09-03/hour=05/_manifest.json

Durability contract -- the whole point of the class:

    ``put()`` returns only when the bytes are durable. For ``LocalStore`` that
    means the file is fsynced AND its parent directory is fsynced (a rename is
    not durable until the directory entry is, so a crash could otherwise leave a
    file that vanishes on reboot). For a future ``S3Store`` it means the upload
    was acknowledged.

    Retention keys deletion off the manifest a ``put()`` produced, so a ``put()``
    that returns early would let the database delete rows the archive does not
    actually have. This is the one method in the package that must not be
    optimistic.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import tempfile
from pathlib import Path
from typing import Protocol, runtime_checkable

log = logging.getLogger(__name__)


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def fsync_dir(path: Path) -> None:
    """fsync a directory so a create/rename inside it survives a crash.

    No-op where the platform refuses to open a directory (Windows): the rename
    itself is atomic there, and NTFS journals the metadata, so the exposure this
    guards against on POSIX does not apply the same way.
    """
    try:
        fd = os.open(path, os.O_RDONLY)
    except (PermissionError, OSError):
        return
    try:
        os.fsync(fd)
    except OSError:  # pragma: no cover - platform dependent
        pass
    finally:
        os.close(fd)


@runtime_checkable
class ArchiveStore(Protocol):
    """The seam. Implement these three to point the archive somewhere else."""

    def put(self, local_path: Path, key: str) -> str:
        """Store ``local_path`` under ``key``. Returns only once durable.

        Returns a URI identifying the stored object. Overwrites an existing key
        (compaction is idempotent -- re-running it on the same hour must be safe).
        """
        ...

    def exists(self, key: str) -> bool:
        ...

    def get_bytes(self, key: str) -> bytes | None:
        """Read an object back, or None if absent.

        Needed because an hour's manifest is read-modify-write: an hour can be
        sealed more than once (a restart mid-hour seals a partial segment, and
        the rest of that hour is sealed later), so each seal must ADD a part
        rather than replace what is already there.
        """
        ...

    def uri(self, key: str) -> str:
        ...


class LocalStore:
    """Archive segments as files under a root directory on this machine.

    Not a stand-in to be replaced -- for the current phase it *is* the system of
    record, and it is written with the same durability care a remote store would
    get. Its one real limitation is that it is a single copy on a single disk;
    that is what the lake step fixes, and it is why the archive tree needs backing
    up alongside noventis.db until then.
    """

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        # Keys are built internally, never from user input, but refuse to escape
        # the root regardless -- a traversal here would write outside the archive.
        p = (self.root / key).resolve()
        if not str(p).startswith(str(self.root.resolve())):
            raise ValueError(f"archive key escapes root: {key!r}")
        return p

    def put(self, local_path: Path, key: str) -> str:
        dst = self._path(key)
        dst.parent.mkdir(parents=True, exist_ok=True)

        # Write to a temp file in the DESTINATION directory (so the rename is on
        # one filesystem and therefore atomic), fsync it, then rename into place.
        # A reader never sees a partial object: it sees the old one or the new one.
        fd, tmp_name = tempfile.mkstemp(dir=dst.parent, prefix=".tmp-", suffix=dst.suffix)
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as out, open(local_path, "rb") as src:
                shutil.copyfileobj(src, out)
                out.flush()
                os.fsync(out.fileno())
            os.replace(tmp, dst)          # atomic
            fsync_dir(dst.parent)         # ...and durable
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

        log.debug("archive put %s (%d bytes)", key, dst.stat().st_size)
        return self.uri(key)

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def get_bytes(self, key: str) -> bytes | None:
        p = self._path(key)
        return p.read_bytes() if p.is_file() else None

    def uri(self, key: str) -> str:
        return self._path(key).as_uri()

    def __repr__(self) -> str:  # pragma: no cover
        return f"LocalStore(root={str(self.root)!r})"


def store_from_env(archive_dir: Path | str) -> ArchiveStore:
    """Build the configured store.

    Only ``local`` exists today. When the lake lands, this is where ``s3`` /
    ``r2`` join -- the writer keeps calling ``put()`` and never learns the
    difference.
    """
    backend = os.getenv("NOVENTIS_ARCHIVE_STORE", "local").strip().lower()
    if backend == "local":
        return LocalStore(Path(archive_dir) / "data")
    raise ValueError(
        f"unknown NOVENTIS_ARCHIVE_STORE={backend!r} (only 'local' is implemented; "
        "a cloud store is the deferred production step)"
    )
