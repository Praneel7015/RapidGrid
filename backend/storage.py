"""Atomic local persistence for the single-worker demonstration service.

Set RAPIDGRID_DATA_DIR to a persistent volume in hosted deployments. SQLite
protects each stored snapshot from partial writes; the API's incident lock
serializes read/modify/write operations within the single application worker.
"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from threading import RLock
from typing import Any

_LOCK = RLock()


def data_path() -> Path:
    directory = Path(os.environ.get("RAPIDGRID_DATA_DIR") or Path(__file__).parent / "data")
    return directory / "rapidgrid.sqlite3"


@contextmanager
def _connection(database: Path | None = None):
    path = database or data_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK:
        connection = sqlite3.connect(path, timeout=30)
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS collections "
                "(name TEXT PRIMARY KEY, payload TEXT NOT NULL)"
            )
            with connection:
                yield connection
        finally:
            connection.close()


def load_collection(name: str, default: Any = None, *, database: Path | None = None) -> Any:
    with _connection(database) as connection:
        row = connection.execute("SELECT payload FROM collections WHERE name = ?", (name,)).fetchone()
    return json.loads(row[0]) if row else default


def save_collection(name: str, value: Any, *, database: Path | None = None) -> None:
    payload = json.dumps(value, ensure_ascii=False)
    with _connection(database) as connection:
        connection.execute(
            "INSERT INTO collections (name, payload) VALUES (?, ?) "
            "ON CONFLICT(name) DO UPDATE SET payload = excluded.payload", (name, payload)
        )


def delete_collections(prefix: str, *, database: Path | None = None) -> None:
    with _connection(database) as connection:
        connection.execute("DELETE FROM collections WHERE substr(name, 1, ?) = ?", (len(prefix), prefix))
