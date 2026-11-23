"""Local persistence for the single-worker demonstration service.

Set RAPIDGRID_DATA_DIR to a persistent volume in hosted deployments.

Incidents and chat messages are stored one row each, so a change writes one
row instead of every record the service has ever held. Small settings
(hospital capacity overrides) stay in the ``collections`` key/value table.

One connection per database file is kept open in WAL mode. ``synchronous`` is
NORMAL: a commit survives a process crash, and an operating-system crash can
lose the last few commits but cannot corrupt the file. That is the right trade
for a showcase, where a forced flush on every change stalled the whole service
for seconds on slow disks.

Run exactly one application process per data directory: each process keeps
its own in-memory view of the incidents.
"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from threading import RLock
from typing import Any, Iterable

_LOCK = RLock()
_CONNECTIONS: dict[Path, sqlite3.Connection] = {}

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS collections (name TEXT PRIMARY KEY, payload TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS incidents (incident_id TEXT PRIMARY KEY, payload TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS chat_messages ("
    "id INTEGER PRIMARY KEY AUTOINCREMENT, incident_id TEXT NOT NULL, payload TEXT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS chat_by_incident ON chat_messages (incident_id, id)",
    "CREATE TABLE IF NOT EXISTS citizens (citizen_id TEXT PRIMARY KEY, payload TEXT NOT NULL)",
)


def data_path() -> Path:
    directory = Path(os.environ.get("RAPIDGRID_DATA_DIR") or Path(__file__).parent / "data")
    return directory / "rapidgrid.sqlite3"


def _open(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Writes run on worker threads; _LOCK serialises every use of the connection.
    connection = sqlite3.connect(path, timeout=30, check_same_thread=False)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        with connection:
            for statement in _SCHEMA:
                connection.execute(statement)
    except Exception:
        connection.close()
        raise
    return connection


@contextmanager
def _connection(database: Path | None = None):
    path = Path(database or data_path())
    with _LOCK:
        connection = _CONNECTIONS.get(path)
        # A data directory replaced underneath us (tests, restores) needs a fresh handle.
        if connection is not None and not path.exists():
            connection.close()
            connection = None
        if connection is None:
            connection = _CONNECTIONS[path] = _open(path)
        with connection:  # one transaction: commit, or roll back on error
            yield connection


def close_all() -> None:
    """Close cached connections (shutdown and tests)."""
    with _LOCK:
        for connection in _CONNECTIONS.values():
            connection.close()
        _CONNECTIONS.clear()


# --- key/value collections ------------------------------------------------------

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


def delete_collection(name: str, *, database: Path | None = None) -> None:
    with _connection(database) as connection:
        connection.execute("DELETE FROM collections WHERE name = ?", (name,))


def delete_collections(prefix: str, *, database: Path | None = None) -> None:
    with _connection(database) as connection:
        connection.execute("DELETE FROM collections WHERE substr(name, 1, ?) = ?", (len(prefix), prefix))


# --- incidents: one row each ----------------------------------------------------

def load_incident_payloads(*, database: Path | None = None) -> list[str]:
    """Stored incidents as JSON text, oldest first."""
    with _connection(database) as connection:
        rows = connection.execute("SELECT payload FROM incidents ORDER BY rowid").fetchall()
    return [row[0] for row in rows]


def write_incident(incident_id: str, payload: str, *, database: Path | None = None) -> None:
    with _connection(database) as connection:
        connection.execute(
            "INSERT INTO incidents (incident_id, payload) VALUES (?, ?) "
            "ON CONFLICT(incident_id) DO UPDATE SET payload = excluded.payload", (incident_id, payload)
        )


def delete_incidents(incident_ids: Iterable[str], *, database: Path | None = None) -> None:
    """Remove incidents together with their chat."""
    ids = [(incident_id,) for incident_id in incident_ids]
    if not ids:
        return
    with _connection(database) as connection:
        connection.executemany("DELETE FROM incidents WHERE incident_id = ?", ids)
        connection.executemany("DELETE FROM chat_messages WHERE incident_id = ?", ids)
        # The pre-row chat layout too, so a later load cannot re-import it.
        connection.executemany("DELETE FROM collections WHERE name = ?", [("chat:" + i,) for (i,) in ids])


def replace_incidents(payloads: dict[str, str], *, database: Path | None = None) -> None:
    """Make the stored set exactly ``payloads`` in one transaction (load, migration, clear)."""
    with _connection(database) as connection:
        connection.execute("DELETE FROM incidents")
        connection.executemany(
            "INSERT INTO incidents (incident_id, payload) VALUES (?, ?)", list(payloads.items())
        )


# --- chat: one row per message --------------------------------------------------

def load_chat(incident_id: str, *, database: Path | None = None) -> list[dict]:
    with _connection(database) as connection:
        rows = connection.execute(
            "SELECT payload FROM chat_messages WHERE incident_id = ? ORDER BY id", (incident_id,)
        ).fetchall()
    return [json.loads(row[0]) for row in rows]


def append_chat(incident_id: str, messages: Iterable[dict], *, database: Path | None = None) -> None:
    rows = [(incident_id, json.dumps(message, ensure_ascii=False)) for message in messages]
    with _connection(database) as connection:
        connection.executemany("INSERT INTO chat_messages (incident_id, payload) VALUES (?, ?)", rows)


def load_chat_migrating(incident_id: str, *, database: Path | None = None) -> list[dict]:
    """Chat rows for an incident, importing a pre-row history once if there are none.

    The emptiness check and the import run in one transaction under the
    storage lock, so concurrent first loads cannot import the same history twice.
    """
    with _connection(database) as connection:
        rows = connection.execute(
            "SELECT payload FROM chat_messages WHERE incident_id = ? ORDER BY id", (incident_id,)
        ).fetchall()
        if rows:
            return [json.loads(row[0]) for row in rows]
        legacy = connection.execute(
            "SELECT payload FROM collections WHERE name = ?", ("chat:" + incident_id,)
        ).fetchone()
        messages = json.loads(legacy[0]) if legacy else []
        if messages:
            connection.executemany(
                "INSERT INTO chat_messages (incident_id, payload) VALUES (?, ?)",
                [(incident_id, json.dumps(m, ensure_ascii=False)) for m in messages],
            )
        return messages


def clear_chat(*, database: Path | None = None) -> None:
    with _connection(database) as connection:
        connection.execute("DELETE FROM chat_messages")


def load_citizen(citizen_id: str, *, database: Path | None = None) -> str | None:
    with _connection(database) as connection:
        row = connection.execute("SELECT payload FROM citizens WHERE citizen_id = ?", (citizen_id,)).fetchone()
    return row[0] if row else None


def insert_citizen(citizen_id: str, payload: str, *, database: Path | None = None) -> bool:
    """Create an account; False if that id already exists (no overwrite)."""
    with _connection(database) as connection:
        cursor = connection.execute(
            "INSERT OR IGNORE INTO citizens (citizen_id, payload) VALUES (?, ?)", (citizen_id, payload))
        return cursor.rowcount == 1


def clear_citizens(*, database: Path | None = None) -> int:
    """Delete every citizen account (after the event; DPDP Act)."""
    with _connection(database) as connection:
        return connection.execute("DELETE FROM citizens").rowcount


def compact(*, database: Path | None = None) -> None:
    """Rewrite the database so deleted rows leave no trace in free pages or
    the write-ahead log (after a purge)."""
    with _connection(database) as connection:
        connection.commit()  # VACUUM cannot run inside a transaction
        connection.execute("VACUUM")
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
