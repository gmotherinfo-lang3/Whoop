"""SQLite storage for records received from the bridge.

De-duplication is by `record_id`, which the bridge derives from the raw frame
bytes. That makes it stable across forwarder retries and across the strap
re-offloading the same record, so `INSERT OR IGNORE` is all that is needed.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
import threading
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger("whoop.db")

# Everything binding a value can raise. OverflowError is not an sqlite3 error
# and is easy to miss: an int outside 64 bits raises it rather than
# InterfaceError, and would otherwise escape as a 500.
UNBINDABLE = (sqlite3.InterfaceError, sqlite3.ProgrammingError, sqlite3.DataError,
              OverflowError, ValueError, TypeError)

# Column order of the records table, minus record_id and rr_json which are
# handled separately. Keeping it in one place stops the tuple and the table
# drifting apart.
RECORD_FIELDS = (
    "received_at", "unix", "packet", "version", "heart_rate",
    "gravity_x", "gravity_y", "gravity_z", "skin_contact", "ppg_green",
    "ppg_red_ir", "spo2_red", "spo2_ir", "skin_temp_raw", "ambient_light",
    "resp_rate_raw", "signal_quality", "raw_hex",
)


# SQLite stores integers in 64 signed bits. Python's do not stop there, and a
# misdecoded frame can produce one that does not fit.
INT64_MIN, INT64_MAX = -(2 ** 63), 2 ** 63 - 1


def _scalar(value: Any) -> Any:
    """Anything SQLite can bind, or None. Booleans become integers."""
    if value is None or isinstance(value, (str, bytes, float)):
        return value
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value if INT64_MIN <= value <= INT64_MAX else None
    return None


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

-- Daily journal: what you did / consumed / how you felt.
CREATE TABLE IF NOT EXISTS journal (
    date       TEXT PRIMARY KEY,   -- YYYY-MM-DD, local day
    tags       TEXT NOT NULL,      -- JSON array of tag strings
    amounts    TEXT NOT NULL,      -- JSON object, e.g. {"alcohol_units": 2}
    notes      TEXT,
    updated_at TEXT NOT NULL
);

-- Bouts detected from sensor data, plus any label you gave them. The
-- confirmed_type column is what the activity classifier trains on.
CREATE TABLE IF NOT EXISTS activities (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    start_unix     INTEGER NOT NULL,
    end_unix       INTEGER NOT NULL,
    detected_type  TEXT,
    confirmed_type TEXT,
    confidence     REAL,
    features       TEXT NOT NULL,   -- JSON feature vector
    source         TEXT NOT NULL,   -- 'auto', 'manual', or 'edited'
    note           TEXT,
    deleted        INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT NOT NULL,
    UNIQUE(start_unix, end_unix)
);
CREATE INDEX IF NOT EXISTS idx_activities_time ON activities(start_unix);

-- Persisted model weights, so learning survives a restart.
CREATE TABLE IF NOT EXISTS model_state (
    name       TEXT PRIMARY KEY,
    payload    TEXT NOT NULL,
    trained_at TEXT NOT NULL,
    n_samples  INTEGER NOT NULL,
    accuracy   REAL
);

-- Latest heartbeat from the laptop bridge. One row; the timestamp is what
-- distinguishes "strap is off" from "the laptop is asleep".
CREATE TABLE IF NOT EXISTS device_status (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    received_at TEXT NOT NULL,
    payload    TEXT NOT NULL
);

-- Timestamped intake, for the caffeine/alcohol overlay. Separate from the
-- day-level journal because the whole point is the time of day.
CREATE TABLE IF NOT EXISTS intake (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    at         TEXT NOT NULL,       -- ISO8601, UTC
    substance  TEXT NOT NULL,       -- 'caffeine' or 'alcohol'
    amount     REAL NOT NULL,       -- mg for caffeine, standard units for alcohol
    label      TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_intake_at ON intake(at);

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
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        """Add columns introduced after a database was first created."""
        have = {r["name"] for r in self._conn.execute("PRAGMA table_info(activities)")}
        for column, ddl in (("note", "TEXT"),
                            ("deleted", "INTEGER NOT NULL DEFAULT 0")):
            if column not in have:
                self._conn.execute(f"ALTER TABLE activities ADD COLUMN {column} {ddl}")
        self._merge_duplicate_activities()

    def _merge_duplicate_activities(self) -> int:
        """Collapse the near-identical rows the old exact-bounds keying left.

        Detection used to key on (start_unix, end_unix), so a night that was
        still growing produced one row per re-detection. Existing databases are
        full of them; new ones will not be. Only auto-detected, undeleted rows
        are touched, and each surviving row is widened to cover the ones it
        absorbs, so nothing a person labelled or removed is affected.
        """
        rows = self._conn.execute(
            "SELECT id, start_unix, end_unix FROM activities "
            "WHERE source='auto' AND deleted=0 ORDER BY start_unix, end_unix").fetchall()
        keep: list[dict[str, int]] = []
        drop: list[int] = []
        for row in rows:
            span = max(1, row["end_unix"] - row["start_unix"])
            merged = False
            for k in keep:
                overlap = min(row["end_unix"], k["end"]) - max(row["start_unix"], k["start"])
                shorter = max(1, min(span, k["end"] - k["start"]))
                if overlap / shorter >= self.OVERLAP_FRACTION:
                    k["start"] = min(k["start"], row["start_unix"])
                    k["end"] = max(k["end"], row["end_unix"])
                    drop.append(row["id"])
                    merged = True
                    break
            if not merged:
                keep.append({"id": row["id"], "start": row["start_unix"],
                             "end": row["end_unix"]})
        if not drop:
            return 0
        # Widen the survivors first, then remove what they absorbed, so the
        # unique index never sees two rows claiming the same bounds.
        self._conn.executemany(
            "DELETE FROM activities WHERE id = ?", [(i,) for i in drop])
        for k in keep:
            self._conn.execute(
                "UPDATE activities SET start_unix=?, end_unix=? WHERE id=?",
                (k["start"], k["end"], k["id"]))
        self._conn.commit()
        log.info("merged %d duplicate detected activities into %d", len(drop), len(keep))
        return len(drop)

    def insert_records(self, records: Iterable[dict[str, Any]]) -> tuple[int, int]:
        """Insert a batch. Returns (received, actually_inserted).

        A record whose fields are the wrong shape must never fail the batch.
        The bridge retries any 5xx forever and only deletes a spooled row once
        the server has acknowledged it, so one unbindable value would stall the
        queue permanently and the strap would silently stop delivering. Bad
        values are dropped to NULL instead; `raw_hex` still carries the truth.
        """
        rows, event_rows, received = [], [], 0
        for r in records:
            received += 1
            rid = _scalar(r.get("record_id"))
            if not rid or not isinstance(rid, str):
                continue
            if r.get("packet") == "EVENT":
                event_rows.append((rid, _scalar(r.get("event")),
                                   _scalar(r.get("event_time")),
                                   _scalar(r.get("received_at"))))
                continue
            rr = r.get("rr_intervals_ms")
            rr_json = json.dumps(rr) if isinstance(rr, (list, tuple)) and rr else None
            rows.append((rid, ) + tuple(_scalar(r.get(f)) for f in RECORD_FIELDS[:5])
                        + (rr_json, )
                        + tuple(_scalar(r.get(f)) for f in RECORD_FIELDS[5:]))

        with self._lock:
            cur = self._conn.cursor()
            before = self._total(cur)
            self._insert_many(
                cur, "INSERT OR IGNORE INTO records VALUES (" + ",".join("?" * 20) + ")", rows)
            self._insert_many(cur, "INSERT OR IGNORE INTO events VALUES (?,?,?,?)", event_rows)
            inserted = self._total(cur) - before
            cur.execute("INSERT INTO ingest_log (at, received, inserted) "
                        "VALUES (datetime('now'), ?, ?)", (received, inserted))
            self._conn.commit()
        return received, inserted

    @staticmethod
    def _insert_many(cur, sql: str, rows: list) -> None:
        """executemany, falling back to row-at-a-time so one bad row cannot
        take the other forty-nine down with it."""
        if not rows:
            return
        try:
            cur.executemany(sql, rows)
        except UNBINDABLE:
            for row in rows:
                try:
                    cur.execute(sql, row)
                except UNBINDABLE:
                    log.warning("dropping a record the database cannot store")

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

    # --- journal ------------------------------------------------------------
    def put_journal(self, date: str, tags: list[str], amounts: dict[str, float],
                    notes: str = "") -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO journal (date, tags, amounts, notes, updated_at) "
                "VALUES (?,?,?,?,datetime('now')) "
                "ON CONFLICT(date) DO UPDATE SET tags=excluded.tags, "
                "amounts=excluded.amounts, notes=excluded.notes, "
                "updated_at=excluded.updated_at",
                (date, json.dumps(sorted(set(tags))), json.dumps(amounts), notes))
            self._conn.commit()

    def get_journal(self, date: str) -> dict[str, Any] | None:
        with self._lock:
            r = self._conn.execute("SELECT * FROM journal WHERE date=?", (date,)).fetchone()
        return self._journal_row(r) if r else None

    def journal_range(self, start_date: str, end_date: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM journal WHERE date >= ? AND date <= ? ORDER BY date",
                (start_date, end_date)).fetchall()
        return [self._journal_row(r) for r in rows]

    @staticmethod
    def _journal_row(r: sqlite3.Row) -> dict[str, Any]:
        d = dict(r)
        d["tags"] = json.loads(d["tags"] or "[]")
        d["amounts"] = json.loads(d["amounts"] or "{}")
        return d

    def all_tags(self) -> list[str]:
        seen: set[str] = set()
        with self._lock:
            for (raw,) in self._conn.execute("SELECT tags FROM journal"):
                seen.update(json.loads(raw or "[]"))
        return sorted(seen)

    # --- activities ---------------------------------------------------------
    def upsert_activity(self, start_unix: int, end_unix: int, detected_type: str | None,
                        confidence: float | None, features: dict[str, float],
                        source: str = "auto", note: str | None = None) -> int:
        """Insert or refresh a detected bout. An existing bout keeps its label.

        Matching is by OVERLAP, not by exact bounds. Detection re-runs while
        the strap is still streaming, and each run sees a slightly longer
        night: 00:00-06:24, then 00:00-06:26, then 00:00-06:27. Keying on
        (start, end) made every one of those a different row, so a single
        night's sleep piled up as a dozen near-identical entries and the list
        grew all day. The same night is one bout, and re-detection should
        extend it rather than add another.
        """
        with self._lock:
            matches = self._overlapping_auto(start_unix, end_unix)
            if matches:
                # A later, longer reading can span fragments an earlier pass
                # split the night into, so every row it covers is absorbed --
                # otherwise the leftovers survive as duplicates.
                keep = matches[0]
                lo = min([start_unix] + [m["start_unix"] for m in matches])
                hi = max([end_unix] + [m["end_unix"] for m in matches])
                if len(matches) > 1:
                    self._conn.executemany(
                        "DELETE FROM activities WHERE id = ?",
                        [(m["id"],) for m in matches[1:]])
                self._conn.execute(
                    "UPDATE activities SET start_unix=?, end_unix=?, "
                    "detected_type=?, confidence=?, features=? WHERE id=?",
                    (lo, hi, detected_type, confidence, json.dumps(features), keep["id"]))
                self._conn.commit()
                return keep["id"]

            cur = self._conn.execute(
                "INSERT OR IGNORE INTO activities (start_unix, end_unix, detected_type, "
                "confidence, features, source, note, created_at) "
                "VALUES (?,?,?,?,?,?,?,datetime('now'))",
                (start_unix, end_unix, detected_type, confidence,
                 json.dumps(features), source, note))
            self._conn.commit()
            if cur.lastrowid:
                return cur.lastrowid
            row = self._conn.execute(
                "SELECT id FROM activities WHERE start_unix=? AND end_unix=?",
                (start_unix, end_unix)).fetchone()
            return row["id"]

    # A re-detected bout has to overlap an existing one by this much of the
    # shorter of the two before they are treated as the same event. High
    # enough that a workout starting right after a nap stays separate; low
    # enough that a night still growing minute by minute keeps matching.
    OVERLAP_FRACTION = 0.5

    def _overlapping_auto(self, start_unix: int, end_unix: int) -> list[sqlite3.Row]:
        """Auto-detected bouts this one is a re-reading of, best match first.

        A user's own row is never matched: an edit or a deletion has to win
        over re-detection, and silently absorbing a manual entry would undo it.
        """
        rows = self._conn.execute(
            "SELECT * FROM activities WHERE source='auto' AND deleted=0 "
            "AND start_unix < ? AND end_unix > ? ORDER BY start_unix",
            (end_unix, start_unix)).fetchall()
        span = max(1, end_unix - start_unix)
        scored = []
        for row in rows:
            overlap = min(end_unix, row["end_unix"]) - max(start_unix, row["start_unix"])
            shorter = max(1, min(span, row["end_unix"] - row["start_unix"]))
            fraction = overlap / shorter
            if fraction >= self.OVERLAP_FRACTION:
                scored.append((fraction, row))
        scored.sort(key=lambda pair: -pair[0])
        return [row for _, row in scored]

    def label_activity(self, activity_id: int, confirmed_type: str) -> bool:
        return self.update_activity(activity_id, confirmed_type=confirmed_type)

    def update_activity(self, activity_id: int, *, confirmed_type: str | None = None,
                        start_unix: int | None = None, end_unix: int | None = None,
                        note: str | None = None) -> bool:
        """Edit a detected bout: retype it, correct its times, or annotate it.

        Any edit marks the row 'edited', which stops re-detection overwriting it.
        """
        sets, args = [], []
        if confirmed_type is not None:
            sets.append("confirmed_type=?"); args.append(confirmed_type)
        if start_unix is not None:
            sets.append("start_unix=?"); args.append(start_unix)
        if end_unix is not None:
            sets.append("end_unix=?"); args.append(end_unix)
        if note is not None:
            sets.append("note=?"); args.append(note)
        if not sets:
            return False
        sets.append("source=CASE WHEN source='manual' THEN 'manual' ELSE 'edited' END")
        args.append(activity_id)
        with self._lock:
            cur = self._conn.execute(
                f"UPDATE activities SET {', '.join(sets)} WHERE id=? AND deleted=0", args)
            self._conn.commit()
            return cur.rowcount > 0

    def delete_activity(self, activity_id: int, hard: bool = False) -> bool:
        """Remove a bout. Soft by default, so re-detection cannot bring it back."""
        with self._lock:
            if hard:
                cur = self._conn.execute("DELETE FROM activities WHERE id=?", (activity_id,))
            else:
                cur = self._conn.execute(
                    "UPDATE activities SET deleted=1 WHERE id=?", (activity_id,))
            self._conn.commit()
            return cur.rowcount > 0

    def restore_activity(self, activity_id: int) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE activities SET deleted=0 WHERE id=?", (activity_id,))
            self._conn.commit()
            return cur.rowcount > 0

    def add_manual_activity(self, start_unix: int, end_unix: int, activity_type: str,
                            note: str = "") -> int:
        """Log something the strap did not detect, or that happened off-wrist."""
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO activities (start_unix, end_unix, detected_type, "
                "confirmed_type, confidence, features, source, note, created_at) "
                "VALUES (?,?,NULL,?,NULL,'{}','manual',?,datetime('now')) "
                "ON CONFLICT(start_unix, end_unix) DO UPDATE SET "
                "confirmed_type=excluded.confirmed_type, note=excluded.note, "
                "source='manual', deleted=0",
                (start_unix, end_unix, activity_type, note))
            self._conn.commit()
            if cur.lastrowid:
                return cur.lastrowid
            return self._conn.execute(
                "SELECT id FROM activities WHERE start_unix=? AND end_unix=?",
                (start_unix, end_unix)).fetchone()["id"]

    def activities_range(self, start_unix: int, end_unix: int) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM activities WHERE start_unix >= ? AND start_unix < ? "
                "AND deleted=0 ORDER BY start_unix", (start_unix, end_unix)).fetchall()
        return [self._activity_row(r) for r in rows]

    def labelled_activities(self) -> list[dict[str, Any]]:
        """Every bout the user has confirmed -- the classifier's training set."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM activities WHERE confirmed_type IS NOT NULL "
                "AND deleted=0 AND source != 'manual' ORDER BY start_unix").fetchall()
        return [self._activity_row(r) for r in rows]

    @staticmethod
    def _activity_row(r: sqlite3.Row) -> dict[str, Any]:
        d = dict(r)
        d["features"] = json.loads(d["features"] or "{}")
        return d

    # --- models -------------------------------------------------------------
    def save_model(self, name: str, payload: dict[str, Any], n_samples: int,
                   accuracy: float | None = None) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO model_state (name, payload, trained_at, n_samples, accuracy) "
                "VALUES (?,?,datetime('now'),?,?) "
                "ON CONFLICT(name) DO UPDATE SET payload=excluded.payload, "
                "trained_at=excluded.trained_at, n_samples=excluded.n_samples, "
                "accuracy=excluded.accuracy",
                (name, json.dumps(payload), n_samples, accuracy))
            self._conn.commit()

    def load_model(self, name: str) -> dict[str, Any] | None:
        with self._lock:
            r = self._conn.execute(
                "SELECT * FROM model_state WHERE name=?", (name,)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["payload"] = json.loads(d["payload"])
        return d

    # --- intake -------------------------------------------------------------
    def add_intake(self, at: str, substance: str, amount: float,
                   label: str = "") -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO intake (at, substance, amount, label, created_at) "
                "VALUES (?,?,?,?,datetime('now'))", (at, substance, amount, label))
            self._conn.commit()
            return cur.lastrowid

    def intake_between(self, start_iso: str, end_iso: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM intake WHERE at >= ? AND at <= ? ORDER BY at",
                (start_iso, end_iso)).fetchall()
        return [dict(r) for r in rows]

    def delete_intake(self, intake_id: int) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM intake WHERE id=?", (intake_id,))
            self._conn.commit()
            return cur.rowcount > 0

    # --- device status ------------------------------------------------------
    def put_device_status(self, payload: dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO device_status (id, received_at, payload) "
                "VALUES (1, ?, ?) ON CONFLICT(id) DO UPDATE SET "
                "received_at=excluded.received_at, payload=excluded.payload",
                (datetime.now(timezone.utc).isoformat(), json.dumps(payload)))
            self._conn.commit()

    def get_device_status(self) -> dict[str, Any] | None:
        with self._lock:
            r = self._conn.execute("SELECT * FROM device_status WHERE id=1").fetchone()
        if not r:
            return None
        return {"received_at": r["received_at"], **json.loads(r["payload"])}

    def close(self) -> None:
        with self._lock:
            self._conn.close()
