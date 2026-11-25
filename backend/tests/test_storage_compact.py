"""compact() must not hold the shared storage lock while it retries a busy checkpoint."""

import sqlite3
import threading
import time

import fleet  # noqa: F401  (keeps the app import order the other tests use)
import storage


def test_a_request_is_served_while_a_checkpoint_is_blocked(tmp_path):
    database = tmp_path / "compact.sqlite3"
    storage.save_collection("units", {"u1": 1}, database=database)

    # A reader in another connection keeps the write-ahead log in use, so the
    # checkpoint waits on it. The shared storage lock must not be held meanwhile.
    reader = sqlite3.connect(database)
    reader.execute("BEGIN")
    reader.execute("SELECT * FROM collections").fetchall()

    errors = []

    def purge():
        try:
            storage.compact(database=database, attempts=1)
        except storage.CompactIncomplete as reason:
            errors.append(reason)

    worker = threading.Thread(target=purge)
    worker.start()
    time.sleep(0.05)  # let the compaction reach its blocked checkpoint

    outcome = {}
    handler = threading.Thread(
        target=lambda: outcome.update(value=storage.load_collection("units", database=database)))
    started = time.monotonic()
    handler.start()
    handler.join(timeout=5)
    waited = time.monotonic() - started
    try:
        assert not handler.is_alive(), "a request waited on the compaction"
        assert outcome["value"] == {"u1": 1}
        assert waited < 2
    finally:
        worker.join(timeout=30)
        reader.rollback()
        reader.close()
    assert len(errors) == 1  # a busy checkpoint never reports success


def test_compact_reports_success_once_the_log_is_truncated(tmp_path, monkeypatch):
    database = tmp_path / "compact_ok.sqlite3"
    storage.save_collection("units", {"u1": 1}, database=database)
    storage.delete_collections("units", database=database)

    monkeypatch.setattr(time, "sleep", lambda seconds: None)
    storage.compact(database=database)  # no exception: the log was emptied
    assert storage.load_collection("units", default="gone", database=database) == "gone"
