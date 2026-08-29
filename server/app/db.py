"""SQLite storage for records received from the bridge.

De-duplication is by `record_id`, which the bridge derives from the raw frame
bytes. That makes it stable across forwarder retries and across the strap
re-offloading the same record, so `INSERT OR IGNORE` is all that is needed.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
    record_id      TEXT PRIMARY KEY,
    received_at    TEXT,
    device_unix    INTEGER,
    packet         TEXT,
    version        INTEGER,
    heart_rate     INTEGER,
    rr_json        TEXT,
    gravity_x      REAL,
    gravity_y      REAL,
    gravity_z      REAL,
    skin_contact   INTEGER,
    ppg_green      INTEGER,
    ppg_red_ir     INTEGER,
    spo2_red       INTEGER,
    spo2_ir        INTEGER,
    skin_temp_raw  INTEGER,
    ambient_light  INTEGER,
    resp_rate_raw  INTEGER,
    signal_quality INTEGER,
    raw_hex        TEXT
);
CREATE INDEX IF NOT EXISTS idx_records_time ON records(device_unix);

CREATE TABLE IF NOT EXISTS events (
    record_id  TEXT PRIMARY KEY,
    event      INTEGER,
    event_time TEXT,
    received_at TEXT
);

CREATE TABLE IF NOT EXISTS ingest_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    at          TEXT NOT NULL,
    received    INTEGER NOT NULL,
    inserted    INTEGER NOT NULL
);
"""

NUMERIC_COLUMNS = (
    "heart_rate", "gravity_x", "gravity_y", "gravity_z", "skin_contact",
    "ppg_green", "ppg_red_ir", "spo2_red", "spo2_ir", "skin_temp_raw",
    "ambient_light", "resp_rate_raw", "signal_quality",
)


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def insert_records(self, records: Iterable[dict[str, Any]]) -> tuple[int, int]:
        """Insert a batch. Returns (received, actually_inserted)."""
        rows, event_rows, received = [], [], 0
        for r in records:
            received += 1
            rid = r.get("record_id")
            if not rid:
                continue
            if r.get("packet") == "EVENT":
                event_rows.append((rid, r.get("event"), r.get("event_time"),
                                   r.get("received_at")))
                continue
            rr = r.get("rr_intervals_ms")
            rows.append((
                rid, r.get("received_at"), r.get("unix"), r.get("packet"),
                r.get("version"), r.get("heart_rate"),
                json.dumps(rr) if rr else None,
                r.get("gravity_x"), r.get("gravity_y"), r.get("gravity_z"),
                r.get("skin_contact"), r.get("ppg_green"), r.get("ppg_red_ir"),
                r.get("spo2_red"), r.get("spo2_ir"), r.get("skin_temp_raw"),
                r.get("ambient_light"), r.get("resp_rate_raw"),
                r.get("signal_quality"), r.get("raw_hex"),
            ))

        with self._lock:
            cur = self._conn.cursor()
            before = self._total(cur)
            if rows:
                cur.executemany(
                    "INSERT OR IGNORE INTO records VALUES (" + ",".join("?" * 20) + ")", rows)
            if event_rows:
                cur.executemany(
                    "INSERT OR IGNORE INTO events VALUES (?,?,?,?)", event_rows)
            inserted = self._total(cur) - before
            cur.execute("INSERT INTO ingest_log (at, received, inserted) "
                        "VALUES (datetime('now'), ?, ?)", (received, inserted))
            self._conn.commit()
        return received, inserted

    @staticmethod
    def _total(cur) -> int:
        a = cur.execute("SELECT COUNT(*) FROM records").fetchone()[0]
        b = cur.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        return a + b

    def range(self, start_unix: int, end_unix: int) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM records WHERE device_unix >= ? AND device_unix < ? "
                "ORDER BY device_unix", (start_unix, end_unix)).fetchall()
        return [self._row(r) for r in rows]

    @staticmethod
    def _row(r: sqlite3.Row) -> dict[str, Any]:
        d = dict(r)
        d["rr_intervals_ms"] = json.loads(d.pop("rr_json") or "null") or []
        return d

    def stats(self) -> dict[str, Any]:
        with self._lock:
            c = self._conn.execute(
                "SELECT COUNT(*) n, MIN(device_unix) lo, MAX(device_unix) hi "
                "FROM records WHERE device_unix IS NOT NULL").fetchone()
            events = self._conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            last = self._conn.execute(
                "SELECT at, received, inserted FROM ingest_log "
                "ORDER BY id DESC LIMIT 1").fetchone()
        return {
            "records": c["n"], "events": events,
            "first_unix": c["lo"], "last_unix": c["hi"],
            "last_ingest": dict(last) if last else None,
        }

    def close(self) -> None:
        with self._lock:
            self._conn.close()
