"""Who may sign in, and whose data is whose.

Two stores, deliberately:

* **accounts.db** — users, sessions, invites, paired laptops. Identity only.
* **data-<user>.db** — one file per person, holding their records, journal,
  activities and everything derived from them.

Splitting them that way is the point. The alternative — one database with a
`user_id` column on every table — makes correct isolation depend on all forty
or so queries carrying the right WHERE clause, and the failure mode of
forgetting one is showing a family member somebody else's heart rate. Here
there is no query that *could* return another person's row, because their rows
are not in the file. It costs a few open handles and rules out whole-household
queries; for a household server that is a good trade.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .auth import (INVITE_DAYS, PAIRING_MINUTES, SESSION_DAYS, hash_password,
                   new_pairing_code, new_token, normalise_code, normalise_email,
                   token_hash, verify_password, waste_time_like_a_real_login)

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    email         TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    display_name  TEXT NOT NULL DEFAULT '',
    timezone      TEXT NOT NULL DEFAULT '',
    role          TEXT NOT NULL DEFAULT 'member',
    max_hr        REAL,
    age           REAL,
    sex           TEXT NOT NULL DEFAULT '',
    sleep_need_h  REAL,
    disabled      INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL
);

-- Only the hash of a token is stored, here and everywhere below. A session
-- cookie, an invite link and a device token are all bearer credentials: a
-- database backup must not contain working ones.
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    last_seen  TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    user_agent TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);

CREATE TABLE IF NOT EXISTS invites (
    token_hash TEXT PRIMARY KEY,
    created_by INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    label      TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used_at    TEXT,
    used_by    INTEGER REFERENCES users(id) ON DELETE SET NULL
);

-- A paired laptop. Its own token replaces the one shared ingest secret, so
-- revoking one laptop does not lock out every other.
CREATE TABLE IF NOT EXISTS devices (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name       TEXT NOT NULL DEFAULT '',
    token_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    last_seen  TEXT,
    revoked    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_devices_user ON devices(user_id);

CREATE TABLE IF NOT EXISTS pairing_codes (
    code_hash  TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used_at    TEXT,
    device_id  INTEGER REFERENCES devices(id) ON DELETE SET NULL
);
"""

PROFILE_FIELDS = ("display_name", "timezone", "max_hr", "age", "sex", "sleep_need_h")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.replace(microsecond=0).isoformat()


def _expired(value: str | None, now: datetime | None = None) -> bool:
    if not value:
        return True
    try:
        return datetime.fromisoformat(value) <= (now or _now())
    except ValueError:
        return True


class Accounts:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    # --- users -------------------------------------------------------------
    def count(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]

    def needs_owner(self) -> bool:
        """True before anyone has signed up: the first visit sets up the owner."""
        return self.count() == 0

    def create_user(self, email: str, password: str, *, display_name: str = "",
                    timezone_name: str = "", role: str = "member") -> dict[str, Any]:
        email = normalise_email(email)
        with self._lock:
            if self._conn.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone():
                raise ValueError("That address already has an account here.")
            cur = self._conn.execute(
                "INSERT INTO users (email, password_hash, display_name, timezone, "
                "role, created_at) VALUES (?,?,?,?,?,?)",
                (email, hash_password(password), display_name.strip(),
                 timezone_name.strip(), role, _iso(_now())))
            self._conn.commit()
            return self._user_by_id(cur.lastrowid)

    def authenticate(self, email: str, password: str) -> dict[str, Any] | None:
        """The user, or None. Costs the same either way, on purpose."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM users WHERE email=?", (normalise_email(email),)).fetchone()
        if row is None:
            waste_time_like_a_real_login()
            return None
        if not verify_password(password, row["password_hash"]):
            return None
        if row["disabled"]:
            return None
        return self._public(row)

    def user(self, user_id: int) -> dict[str, Any] | None:
        with self._lock:
            return self._user_by_id(user_id)

    def _user_by_id(self, user_id: int) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        return self._public(row) if row else None

    def users(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM users ORDER BY id").fetchall()
        return [self._public(r) for r in rows]

    @staticmethod
    def _public(row: sqlite3.Row) -> dict[str, Any]:
        """Never includes the password hash. Nothing outside needs it."""
        d = dict(row)
        d.pop("password_hash", None)
        d["disabled"] = bool(d["disabled"])
        return d

    def update_profile(self, user_id: int, **fields: Any) -> dict[str, Any] | None:
        sets = {k: v for k, v in fields.items() if k in PROFILE_FIELDS}
        if not sets:
            return self.user(user_id)
        with self._lock:
            self._conn.execute(
                f"UPDATE users SET {', '.join(f'{k}=?' for k in sets)} WHERE id=?",
                (*sets.values(), user_id))
            self._conn.commit()
            return self._user_by_id(user_id)

    def set_password(self, user_id: int, password: str) -> None:
        with self._lock:
            self._conn.execute("UPDATE users SET password_hash=? WHERE id=?",
                               (hash_password(password), user_id))
            # Changing a password ends every other session: that is the whole
            # point of changing it when you think someone else has it.
            self._conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
            self._conn.commit()

    def set_disabled(self, user_id: int, disabled: bool) -> None:
        with self._lock:
            self._conn.execute("UPDATE users SET disabled=? WHERE id=?",
                               (1 if disabled else 0, user_id))
            if disabled:
                self._conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
            self._conn.commit()

    # --- sessions ----------------------------------------------------------
    def start_session(self, user_id: int, user_agent: str = "") -> str:
        token = new_token()
        now = _now()
        with self._lock:
            self._conn.execute(
                "INSERT INTO sessions (token_hash, user_id, created_at, last_seen, "
                "expires_at, user_agent) VALUES (?,?,?,?,?,?)",
                (token_hash(token), user_id, _iso(now), _iso(now),
                 _iso(now + timedelta(days=SESSION_DAYS)), user_agent[:200]))
            self._conn.commit()
        return token

    def session_user(self, token: str | None) -> dict[str, Any] | None:
        if not token:
            return None
        digest = token_hash(token)
        with self._lock:
            row = self._conn.execute(
                "SELECT s.expires_at, u.* FROM sessions s JOIN users u ON u.id = s.user_id "
                "WHERE s.token_hash = ?", (digest,)).fetchone()
            if row is None:
                return None
            if _expired(row["expires_at"]) or row["disabled"]:
                self._conn.execute("DELETE FROM sessions WHERE token_hash=?", (digest,))
                self._conn.commit()
                return None
            self._conn.execute("UPDATE sessions SET last_seen=? WHERE token_hash=?",
                               (_iso(_now()), digest))
            self._conn.commit()
        user = dict(row)
        user.pop("expires_at", None)
        user.pop("password_hash", None)
        user["disabled"] = bool(user["disabled"])
        return user

    def end_session(self, token: str | None) -> None:
        if not token:
            return
        with self._lock:
            self._conn.execute("DELETE FROM sessions WHERE token_hash=?",
                               (token_hash(token),))
            self._conn.commit()

    def sessions_for(self, user_id: int) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT created_at, last_seen, expires_at, user_agent FROM sessions "
                "WHERE user_id=? ORDER BY last_seen DESC", (user_id,)).fetchall()
        return [dict(r) for r in rows]

    # --- invites -----------------------------------------------------------
    def create_invite(self, created_by: int, label: str = "") -> str:
        token = new_token()
        now = _now()
        with self._lock:
            self._conn.execute(
                "INSERT INTO invites (token_hash, created_by, label, created_at, "
                "expires_at) VALUES (?,?,?,?,?)",
                (token_hash(token), created_by, label.strip()[:80], _iso(now),
                 _iso(now + timedelta(days=INVITE_DAYS))))
            self._conn.commit()
        return token

    def invite_status(self, token: str) -> tuple[bool, str]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM invites WHERE token_hash=?", (token_hash(token),)).fetchone()
        if row is None:
            return False, "That invite link is not valid."
        if row["used_at"]:
            return False, "That invite has already been used."
        if _expired(row["expires_at"]):
            return False, "That invite has expired. Ask for a new one."
        return True, ""

    def redeem_invite(self, token: str, email: str, password: str,
                      display_name: str = "", timezone_name: str = "") -> dict[str, Any]:
        ok, why = self.invite_status(token)
        if not ok:
            raise ValueError(why)
        user = self.create_user(email, password, display_name=display_name,
                                timezone_name=timezone_name, role="member")
        with self._lock:
            # Conditional on still being unused, so two people opening the same
            # link at once cannot both get through.
            cur = self._conn.execute(
                "UPDATE invites SET used_at=?, used_by=? "
                "WHERE token_hash=? AND used_at IS NULL",
                (_iso(_now()), user["id"], token_hash(token)))
            self._conn.commit()
            if cur.rowcount == 0:
                self._conn.execute("DELETE FROM users WHERE id=?", (user["id"],))
                self._conn.commit()
                raise ValueError("That invite has already been used.")
        return user

    def invites(self, created_by: int) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT label, created_at, expires_at, used_at, used_by FROM invites "
                "WHERE created_by=? ORDER BY created_at DESC LIMIT 50",
                (created_by,)).fetchall()
        return [{**dict(r), "expired": _expired(r["expires_at"])} for r in rows]

    # --- devices and pairing ----------------------------------------------
    def start_pairing(self, user_id: int) -> tuple[str, str]:
        """A short code to type into the laptop. Returns (code, expires_at)."""
        code = new_pairing_code()
        now = _now()
        expires = now + timedelta(minutes=PAIRING_MINUTES)
        with self._lock:
            # One live code per person: a fresh one invalidates the last, so a
            # code read off a screen an hour ago cannot still be used.
            self._conn.execute(
                "DELETE FROM pairing_codes WHERE user_id=? AND used_at IS NULL",
                (user_id,))
            self._conn.execute(
                "INSERT INTO pairing_codes (code_hash, user_id, created_at, expires_at) "
                "VALUES (?,?,?,?)",
                (token_hash(code), user_id, _iso(now), _iso(expires)))
            self._conn.commit()
        return code, _iso(expires)

    def claim_pairing(self, code: str, device_name: str = "") -> dict[str, Any] | None:
        """Exchange a code for a device token. Returns None if it is no good."""
        digest = token_hash(normalise_code(code))
        token = new_token()
        now = _now()
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM pairing_codes WHERE code_hash=?", (digest,)).fetchone()
            if row is None or row["used_at"] or _expired(row["expires_at"], now):
                return None
            cur = self._conn.execute(
                "INSERT INTO devices (user_id, name, token_hash, created_at) "
                "VALUES (?,?,?,?)",
                (row["user_id"], (device_name or "Laptop").strip()[:60],
                 token_hash(token), _iso(now)))
            device_id = cur.lastrowid
            # Marking used is conditional for the same reason as invites: two
            # laptops racing the same code must not both get a token.
            used = self._conn.execute(
                "UPDATE pairing_codes SET used_at=?, device_id=? "
                "WHERE code_hash=? AND used_at IS NULL",
                (_iso(now), device_id, digest))
            if used.rowcount == 0:
                self._conn.execute("DELETE FROM devices WHERE id=?", (device_id,))
                self._conn.commit()
                return None
            self._conn.commit()
            user = self._user_by_id(row["user_id"])
        return {"token": token, "device_id": device_id, "user": user}

    def device_user(self, token: str | None) -> dict[str, Any] | None:
        """The account a device token belongs to, or None."""
        if not token:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT d.id AS device_id, d.user_id, d.revoked FROM devices d "
                "WHERE d.token_hash=?", (token_hash(token),)).fetchone()
            if row is None or row["revoked"]:
                return None
            self._conn.execute("UPDATE devices SET last_seen=? WHERE id=?",
                               (_iso(_now()), row["device_id"]))
            self._conn.commit()
            user = self._user_by_id(row["user_id"])
        if user is None or user["disabled"]:
            return None
        return {**user, "device_id": row["device_id"]}

    def devices(self, user_id: int) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, name, created_at, last_seen, revoked FROM devices "
                "WHERE user_id=? ORDER BY created_at", (user_id,)).fetchall()
        return [{**dict(r), "revoked": bool(r["revoked"])} for r in rows]

    def revoke_device(self, user_id: int, device_id: int) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE devices SET revoked=1 WHERE id=? AND user_id=?",
                (device_id, user_id))
            self._conn.commit()
        return cur.rowcount > 0

    def prune(self) -> None:
        """Clear out what has expired. Cheap, and keeps the tables honest."""
        now = _iso(_now())
        with self._lock:
            self._conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (now,))
            self._conn.execute(
                "DELETE FROM pairing_codes WHERE used_at IS NULL AND expires_at <= ?",
                (now,))
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()
