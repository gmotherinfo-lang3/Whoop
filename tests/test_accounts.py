"""Accounts, sessions, invites and pairing.

This is the part of the app where being wrong is expensive, so the tests are
about the properties that matter rather than the happy path: a token that
never appears in the database, a login that costs the same whether or not the
account exists, an invite that cannot be used twice.
"""
import time

import pytest

from server.app.accounts import Accounts
from server.app.auth import (RateLimiter, hash_password, new_pairing_code,
                             normalise_code, password_problem, email_problem,
                             token_hash, verify_password)


@pytest.fixture
def acc(tmp_path):
    return Accounts(tmp_path / "accounts.db")


@pytest.fixture
def owner(acc):
    return acc.create_user("me@example.com", "a-decent-passphrase",
                           display_name="Me", role="owner")


# --- passwords --------------------------------------------------------------
def test_a_password_round_trips():
    h = hash_password("a-decent-passphrase")
    assert verify_password("a-decent-passphrase", h)
    assert not verify_password("a-decent-passphras", h)


def test_the_password_is_not_recoverable_from_what_is_stored():
    h = hash_password("a-decent-passphrase")
    assert "a-decent-passphrase" not in h
    assert h.startswith("scrypt$")


def test_the_same_password_hashes_differently_each_time():
    """A shared salt would make one cracked hash crack every matching account."""
    assert hash_password("same-password-here") != hash_password("same-password-here")


def test_a_corrupt_stored_hash_is_a_failed_login_not_a_crash():
    for junk in ["", "not-a-hash", "scrypt$x$y$z$aa$bb", "scrypt$1$1$1$zz$zz"]:
        assert verify_password("anything", junk) is False


@pytest.mark.parametrize("bad", ["short", "password", "PASSWORD", "123456789"])
def test_weak_passwords_are_refused(bad):
    assert password_problem(bad)


def test_a_long_passphrase_is_accepted():
    assert password_problem("three word passphrase") is None


@pytest.mark.parametrize("bad", ["", "nope", "a@b", "no spaces@example.com "])
def test_bad_addresses_are_refused(bad):
    assert email_problem(bad)


# --- users ------------------------------------------------------------------
def test_the_first_account_is_the_owner(acc):
    assert acc.needs_owner()
    acc.create_user("me@example.com", "a-decent-passphrase", role="owner")
    assert not acc.needs_owner()


def test_addresses_are_case_and_space_insensitive(acc, owner):
    assert acc.authenticate("  ME@Example.COM ", "a-decent-passphrase")


def test_the_same_address_cannot_register_twice(acc, owner):
    with pytest.raises(ValueError):
        acc.create_user("ME@example.com", "another-passphrase")


def test_a_user_record_never_carries_the_password_hash(acc, owner):
    for record in [owner, acc.user(owner["id"]), *acc.users()]:
        assert "password_hash" not in record


def test_the_wrong_password_does_not_authenticate(acc, owner):
    assert acc.authenticate("me@example.com", "wrong-passphrase") is None


def test_an_unknown_address_costs_what_a_real_one_does(acc, owner):
    """Otherwise the response time says which addresses have accounts."""
    def took(email, password):
        t0 = time.perf_counter(); acc.authenticate(email, password)
        return time.perf_counter() - t0
    unknown = took("nobody@example.com", "a-decent-passphrase")
    known = took("me@example.com", "wrong-passphrase")
    assert unknown > known / 3, f"unknown {unknown*1000:.0f}ms vs known {known*1000:.0f}ms"


def test_a_disabled_account_cannot_sign_in(acc, owner):
    acc.set_disabled(owner["id"], True)
    assert acc.authenticate("me@example.com", "a-decent-passphrase") is None


# --- sessions ---------------------------------------------------------------
def test_a_session_identifies_its_user(acc, owner):
    token = acc.start_session(owner["id"], "iPhone")
    assert acc.session_user(token)["id"] == owner["id"]


def test_the_session_token_is_never_stored(acc, owner):
    token = acc.start_session(owner["id"])
    stored = acc._conn.execute("SELECT token_hash FROM sessions").fetchone()[0]
    assert stored != token and stored == token_hash(token)


def test_a_made_up_token_is_nobody(acc, owner):
    acc.start_session(owner["id"])
    assert acc.session_user("not-a-real-token") is None
    assert acc.session_user(None) is None
    assert acc.session_user("") is None


def test_signing_out_ends_that_session(acc, owner):
    token = acc.start_session(owner["id"])
    acc.end_session(token)
    assert acc.session_user(token) is None


def test_an_expired_session_is_nobody(acc, owner):
    token = acc.start_session(owner["id"])
    acc._conn.execute("UPDATE sessions SET expires_at='2000-01-01T00:00:00+00:00'")
    acc._conn.commit()
    assert acc.session_user(token) is None


def test_changing_the_password_signs_every_session_out(acc, owner):
    """The reason to change it is usually that someone else has it."""
    phone = acc.start_session(owner["id"], "iPhone")
    laptop = acc.start_session(owner["id"], "Laptop")
    acc.set_password(owner["id"], "a-brand-new-passphrase")
    assert acc.session_user(phone) is None and acc.session_user(laptop) is None
    assert acc.authenticate("me@example.com", "a-brand-new-passphrase")


def test_disabling_an_account_signs_it_out(acc, owner):
    token = acc.start_session(owner["id"])
    acc.set_disabled(owner["id"], True)
    assert acc.session_user(token) is None


# --- invites ----------------------------------------------------------------
def test_an_invite_creates_an_account(acc, owner):
    token = acc.create_invite(owner["id"], "brother")
    ok, _ = acc.invite_status(token)
    assert ok
    user = acc.redeem_invite(token, "bro@example.com", "his-own-passphrase")
    assert user["role"] == "member"
    assert acc.authenticate("bro@example.com", "his-own-passphrase")


def test_an_invite_works_only_once(acc, owner):
    token = acc.create_invite(owner["id"])
    acc.redeem_invite(token, "bro@example.com", "his-own-passphrase")
    with pytest.raises(ValueError):
        acc.redeem_invite(token, "someone@example.com", "another-passphrase")


def test_an_expired_invite_is_refused(acc, owner):
    token = acc.create_invite(owner["id"])
    acc._conn.execute("UPDATE invites SET expires_at='2000-01-01T00:00:00+00:00'")
    acc._conn.commit()
    ok, why = acc.invite_status(token)
    assert not ok and "expired" in why


def test_a_made_up_invite_is_refused(acc, owner):
    ok, _ = acc.invite_status("not-a-real-invite")
    assert not ok


def test_a_failed_redemption_leaves_no_half_made_account(acc, owner):
    token = acc.create_invite(owner["id"])
    acc.redeem_invite(token, "bro@example.com", "his-own-passphrase")
    before = len(acc.users())
    with pytest.raises(ValueError):
        acc.redeem_invite(token, "third@example.com", "yet-another-passphrase")
    assert len(acc.users()) == before
    assert acc.authenticate("third@example.com", "yet-another-passphrase") is None


# --- pairing ----------------------------------------------------------------
def test_a_code_pairs_a_laptop_to_its_owner(acc, owner):
    code, _ = acc.start_pairing(owner["id"])
    got = acc.claim_pairing(code, "Windows laptop")
    assert got and got["user"]["id"] == owner["id"]
    assert acc.device_user(got["token"])["id"] == owner["id"]


def test_a_code_can_be_typed_however_it_is_read(acc, owner):
    code, _ = acc.start_pairing(owner["id"])
    messy = " " + code.replace("-", "").lower() + " "
    assert acc.claim_pairing(messy) is not None


def test_a_code_works_only_once(acc, owner):
    code, _ = acc.start_pairing(owner["id"])
    assert acc.claim_pairing(code) is not None
    assert acc.claim_pairing(code) is None


def test_asking_for_a_new_code_kills_the_old_one(acc, owner):
    first, _ = acc.start_pairing(owner["id"])
    acc.start_pairing(owner["id"])
    assert acc.claim_pairing(first) is None


def test_an_expired_code_is_refused(acc, owner):
    code, _ = acc.start_pairing(owner["id"])
    acc._conn.execute("UPDATE pairing_codes SET expires_at='2000-01-01T00:00:00+00:00'")
    acc._conn.commit()
    assert acc.claim_pairing(code) is None


def test_a_made_up_code_is_refused(acc, owner):
    acc.start_pairing(owner["id"])
    assert acc.claim_pairing("AAAA-BBBB") is None


def test_the_device_token_is_never_stored(acc, owner):
    code, _ = acc.start_pairing(owner["id"])
    got = acc.claim_pairing(code)
    stored = acc._conn.execute("SELECT token_hash FROM devices").fetchone()[0]
    assert stored != got["token"] and stored == token_hash(got["token"])


def test_revoking_a_laptop_stops_it_posting(acc, owner):
    code, _ = acc.start_pairing(owner["id"])
    got = acc.claim_pairing(code)
    assert acc.revoke_device(owner["id"], got["device_id"])
    assert acc.device_user(got["token"]) is None


def test_one_person_cannot_revoke_another_persons_laptop(acc, owner):
    brother = acc.create_user("bro@example.com", "his-own-passphrase")
    code, _ = acc.start_pairing(owner["id"])
    mine = acc.claim_pairing(code)
    assert acc.revoke_device(brother["id"], mine["device_id"]) is False
    assert acc.device_user(mine["token"]) is not None


def test_a_laptop_belonging_to_a_disabled_account_stops_posting(acc, owner):
    code, _ = acc.start_pairing(owner["id"])
    got = acc.claim_pairing(code)
    acc.set_disabled(owner["id"], True)
    assert acc.device_user(got["token"]) is None


def test_pairing_codes_avoid_letters_that_look_alike():
    """They get read off one screen and typed into another."""
    for _ in range(50):
        assert not (set(new_pairing_code()) & set("O0I1"))


def test_normalising_a_code_leaves_a_real_one_alone():
    assert normalise_code("K7M2-9QX4") == "K7M2-9QX4"


# --- rate limiting ----------------------------------------------------------
def test_repeated_failures_are_slowed_down():
    limiter = RateLimiter(limit=3, window=60, penalty=60)
    assert limiter.blocked_for("someone") == 0
    for _ in range(3):
        limiter.record_failure("someone")
    assert limiter.blocked_for("someone") > 0


def test_a_success_clears_the_count():
    limiter = RateLimiter(limit=3, window=60, penalty=60)
    limiter.record_failure("someone"); limiter.record_failure("someone")
    limiter.record_success("someone")
    limiter.record_failure("someone")
    assert limiter.blocked_for("someone") == 0


def test_one_persons_failures_do_not_block_another():
    limiter = RateLimiter(limit=2, window=60, penalty=60)
    limiter.record_failure("a"); limiter.record_failure("a")
    assert limiter.blocked_for("a") > 0
    assert limiter.blocked_for("b") == 0
