"""Durable SQLite spool.

The bridge writes every record here first and only deletes it once the cloud
endpoint has acknowledged it. That way a Wi-Fi drop, a laptop sleep, or a 500
from the endpoint costs nothing -- the records are re-sent on reconnect
instead of being lost.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable

_SCHEMA = """
CREATE TABLE IF NOT EXISTS spool (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    queued_at TEXT NOT NULL,
    attempts  INTEGER NOT NULL DEFAULT 0,
    body      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_spool_id ON spool(id);
"""


class Spool:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # WAL keeps writes cheap while a reader is draining the queue.
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def put(self, record: dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO spool (queued_at, body) VALUES (?, ?)",
                (record.get("received_at", ""), json.dumps(record, separators=(",", ":"))),
            )
            self._conn.commit()

    def peek(self, limit: int) -> list[tuple[int, dict[str, Any]]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, body FROM spool ORDER BY id LIMIT ?", (limit,)
            ).fetchall()
        return [(r["id"], json.loads(r["body"])) for r in rows]

    def ack(self, ids: Iterable[int]) -> None:
        ids = list(ids)
        if not ids:
            return
        with self._lock:
            self._conn.executemany("DELETE FROM spool WHERE id = ?", [(i,) for i in ids])
            self._conn.commit()

    def bump_attempts(self, ids: Iterable[int]) -> None:
        ids = list(ids)
        if not ids:
            return
        with self._lock:
            self._conn.executemany(
                "UPDATE spool SET attempts = attempts + 1 WHERE id = ?", [(i,) for i in ids]
            )
            self._conn.commit()

    def depth(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM spool").fetchone()[0]

    def trim(self, max_rows: int) -> int:
        """Drop the oldest rows if the spool exceeds `max_rows`. Returns rows dropped."""
        with self._lock:
            n = self._conn.execute("SELECT COUNT(*) FROM spool").fetchone()[0]
            if n <= max_rows:
                return 0
            excess = n - max_rows
            self._conn.execute(
                "DELETE FROM spool WHERE id IN (SELECT id FROM spool ORDER BY id LIMIT ?)",
                (excess,),
            )
            self._conn.commit()
            return excess

    def close(self) -> None:
        with self._lock:
            self._conn.close()
