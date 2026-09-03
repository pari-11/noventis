"""
backend/archive -- append-only archive of every CRC-valid reading.

The system of record. SQLite is a disposable cache in front of it.

  writer.py  ArchiveWriter -- fourth event-bus subscriber; a trivial async side
             (one put_nowait) and a dedicated thread that owns all file I/O.
  store.py   Where finished segments go. THE SEAM: LocalStore today, a cloud
             store later, with nothing else in the codebase changing.
"""

from .store import ArchiveStore, LocalStore, store_from_env
from .writer import ArchiveWriter, archive_row, scan_spool_file

__all__ = [
    "ArchiveStore",
    "ArchiveWriter",
    "LocalStore",
    "archive_row",
    "scan_spool_file",
    "store_from_env",
]
