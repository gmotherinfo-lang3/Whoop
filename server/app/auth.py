"""Accounts, sessions and invites.

Design decisions worth stating, because this is the part where being wrong is
expensive:

* **scrypt for passwords.** Memory-hard, in the standard library, no new
  dependency to keep current. Parameters below are the interactive-login end of
  the range RFC 9106 and the Go/OpenSSL defaults land on.
* **Tokens are stored hashed.** A session cookie or an invite link is a bearer
  credential: whoever holds it is the user. The database keeps only SHA-256 of
  it, so a leaked backup does not hand over live sessions.
* **Comparisons are constant-time**, and a failed login costs the same whether
  the account exists or not, so the response cannot be used to enumerate who
  has an account here.
* **The first account is the owner.** After that, registration is by invite
  only. An app reachable from the internet with open signup is a door you have
  to keep watching, and this one is for a household.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import time
from dataclasses import dataclass
from typing import Any

# scrypt, at the interactive-login end: ~64 MB and a few hundred ms per hash.
SCRYPT_N = 2 ** 16
SCRYPT_R = 8
SCRYPT_P = 1
SALT_BYTES = 16
KEY_BYTES = 32

SESSION_BYTES = 32
SESSION_DAYS = 90
INVITE_DAYS = 7
PAIRING_MINUTES = 15

MIN_PASSWORD = 10
# Deliberately not a complexity rule. Length is what matters, and rules that
# demand a symbol mostly produce "Password1!" -- worse, and harder to type on a
# phone. This only rejects the handful of passwords that are guessed first.
WORST_PASSWORDS = {
    "password", "password1", "passw0rd", "1234567890", "12345678910",
    "qwertyuiop", "letmein123", "iloveyou1", "admin12345", "welcome123",
    "abc123456", "123456789", "1234567890a", "qwerty12345", "password123",
}

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s.]+\.[^@\s]+$")

# Unambiguous alphabet: no O/0, I/1, so a code read off a screen and typed on a
# laptop cannot be got wrong.
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(SALT_BYTES)
    key = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=SCRYPT_N,
                         r=SCRYPT_R, p=SCRYPT_P, dklen=KEY_BYTES,
                         maxmem=SCRYPT_N * SCRYPT_R * 200)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt.hex()}${key.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_hex, key_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        n, r, p = int(n), int(r), int(p)
        expected = bytes.fromhex(key_hex)
        got = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt_hex),
                             n=n, r=r, p=p, dklen=len(expected),
                             maxmem=n * r * 200)
    except (ValueError, TypeError, MemoryError):
        return False
    return hmac.compare_digest(got, expected)


# A hash of a password nobody has, so a login for an unknown address still does
# the work a real one would. Without it, "no such account" returns in a
# microsecond and "wrong password" in a few hundred milliseconds, which tells
# an attacker exactly which addresses are worth attacking.
_DUMMY_HASH = hash_password(secrets.token_urlsafe(32))


def waste_time_like_a_real_login() -> None:
    verify_password("not-the-password", _DUMMY_HASH)


def password_problem(password: str) -> str | None:
    """Why this password is not acceptable, or None."""
    if len(password) < MIN_PASSWORD:
        return f"Use at least {MIN_PASSWORD} characters — length is what makes a password hard to guess."
    if password.lower() in WORST_PASSWORDS:
        return "That is one of the first passwords anyone tries. Pick another."
    return None


def email_problem(email: str) -> str | None:
    email = email.strip()
    if not email:
        return "An email address is needed — it is what you sign in with."
    if len(email) > 254 or not EMAIL_RE.match(email):
        return "That does not look like an email address."
    return None


def normalise_email(email: str) -> str:
    return email.strip().lower()


def new_token(nbytes: int = SESSION_BYTES) -> str:
    return secrets.token_urlsafe(nbytes)


def token_hash(token: str) -> str:
    """What goes in the database. The token itself never does."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_pairing_code() -> str:
    """A short code a person reads off one screen and types into another."""
    raw = "".join(secrets.choice(CODE_ALPHABET) for _ in range(8))
    return f"{raw[:4]}-{raw[4:]}"


def normalise_code(code: str) -> str:
    """Accept it however it was typed: spaces, lower case, missing dash."""
    cleaned = re.sub(r"[^A-Za-z0-9]", "", code or "").upper()
    return f"{cleaned[:4]}-{cleaned[4:8]}" if len(cleaned) == 8 else cleaned


@dataclass
class Attempt:
    """One rate-limited thing: a login, an invite claim, a pairing attempt."""
    count: int = 0
    first: float = 0.0
    blocked_until: float = 0.0


class RateLimiter:
    """Slows down guessing without locking anyone out for good.

    Deliberately in memory: a restart clearing it is fine, and it keeps the
    hot path off the database. The window is per key -- an email address, or a
    client address for things that have no account attached yet.
    """

    def __init__(self, limit: int = 8, window: float = 300.0, penalty: float = 300.0):
        self.limit, self.window, self.penalty = limit, window, penalty
        self._seen: dict[str, Attempt] = {}

    def blocked_for(self, key: str, now: float | None = None) -> float:
        now = now if now is not None else time.monotonic()
        a = self._seen.get(key)
        if a and a.blocked_until > now:
            return a.blocked_until - now
        return 0.0

    def record_failure(self, key: str, now: float | None = None) -> None:
        now = now if now is not None else time.monotonic()
        a = self._seen.setdefault(key, Attempt(first=now))
        if now - a.first > self.window:
            a.count, a.first = 0, now
        a.count += 1
        if a.count >= self.limit:
            a.blocked_until = now + self.penalty
            a.count, a.first = 0, now

    def record_success(self, key: str) -> None:
        self._seen.pop(key, None)

    def prune(self, now: float | None = None) -> None:
        now = now if now is not None else time.monotonic()
        for key, a in list(self._seen.items()):
            if a.blocked_until < now and now - a.first > self.window:
                del self._seen[key]
