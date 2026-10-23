"""`sqlite3.Connection.serialize()` / `.deserialize()` exist only on Python 3.11+.

The host runs Python 3.9, so every backup / restore goes through these two helpers
instead — they use `Connection.backup()` and a temp file, which work everywhere.
"""
from __future__ import annotations

import os
import sqlite3
import tempfile


def serialize(conn: sqlite3.Connection) -> bytes:
    """Whole database of `conn` as plain SQLite file bytes."""
    handle, tmp = tempfile.mkstemp(suffix=".sqlite3")
    os.close(handle)
    try:
        dest = sqlite3.connect(tmp)
        try:
            conn.backup(dest)
            try:
                dest.execute("PRAGMA journal_mode=DELETE")
            except sqlite3.Error:
                pass
            dest.commit()
        finally:
            dest.close()
        with open(tmp, "rb") as fh:
            return fh.read()
    finally:
        for suffix in ("", "-wal", "-shm", "-journal"):
            try:
                os.unlink(tmp + suffix)
            except OSError:
                pass


def load(conn: sqlite3.Connection, raw: bytes) -> None:
    """Replace the content of `conn` (normally `:memory:`) with SQLite file bytes."""
    handle, tmp = tempfile.mkstemp(suffix=".sqlite3")
    try:
        with os.fdopen(handle, "wb") as fh:
            fh.write(raw)
        src = sqlite3.connect(tmp)
        try:
            src.backup(conn)
        finally:
            src.close()
    finally:
        for suffix in ("", "-wal", "-shm", "-journal"):
            try:
                os.unlink(tmp + suffix)
            except OSError:
                pass
