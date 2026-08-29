"""Bridge self-update. The archive is remote input, so the extraction guard
matters more than the happy path."""
import hashlib
import io
import json
import zipfile
from pathlib import Path

import pytest

from server.app.bridge import build_code_zip, bridge_version, pushed_config, release
from whoop_bridge.config import Config
from whoop_bridge.updater import APPLIABLE, Updater, _safe_members, apply_pending

ROOT = Path(__file__).resolve().parents[1]


def zip_with(entries):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in entries:
            z.writestr(name, data)
    buf.seek(0)
    return zipfile.ZipFile(buf)


# --- extraction guard -------------------------------------------------------
def test_accepts_files_inside_the_bridge():
    zf = zip_with([("whoop_bridge/protocol.py", "x"), ("windows/setup.ps1", "y"),
                   ("pyproject.toml", "z")])
    assert len(_safe_members(zf)) == 3


@pytest.mark.parametrize("name", [
    "../evil.py",
    "whoop_bridge/../../evil.py",
    "/etc/passwd",
    "..\\..\\evil.py",
])
def test_rejects_traversal_and_absolute_paths(name):
    with pytest.raises(ValueError):
        _safe_members(zip_with([(name, "pwned")]))


@pytest.mark.parametrize("name", [
    "config.toml",          # would overwrite the user's credentials
    "whoop-spool.db",       # would destroy unsent data
    "somewhere/else.py",
    ".ssh/authorized_keys",
])
def test_rejects_writes_outside_the_allowed_paths(name):
    with pytest.raises(ValueError):
        _safe_members(zip_with([(name, "x")]))


def test_guard_runs_before_anything_is_written(tmp_path):
    # A mixed archive must extract nothing at all, not partially apply.
    cfg = Config()
    cfg.forward_url = "https://x/ingest"
    up = Updater(cfg, root=tmp_path)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("whoop_bridge/ok.py", "fine")
        z.writestr("../escape.py", "bad")
    with pytest.raises(ValueError):
        up._stage(buf.getvalue(), {"version": "9"}, "deadbeef")
    assert not list((tmp_path / ".update" / "next").rglob("*.py"))


# --- config push ------------------------------------------------------------
def test_pushed_config_applies_only_behaviour():
    cfg = Config()
    cfg.address = "AA:BB"
    cfg.forward_token = "secret"
    up = Updater(cfg)
    changed = up.apply_pushed_config({
        "log_level": "DEBUG", "backfill_interval": 600.0,
        # None of these may be honoured from the server.
        "address": "hijacked", "forward_token": "stolen",
        "forward_url": "https://attacker.test/ingest", "spool_path": "/etc/x",
    })
    assert set(changed) == {"log_level=DEBUG", "backfill_interval=600.0"}
    assert cfg.address == "AA:BB"
    assert cfg.forward_token == "secret"
    assert cfg.forward_url == ""


def test_appliable_never_includes_credentials_or_targets():
    for forbidden in ("address", "forward_url", "forward_token", "hmac_secret",
                      "cf_access_client_id", "cf_access_client_secret", "spool_path",
                      "verify_tls"):
        assert forbidden not in APPLIABLE


def test_unchanged_values_are_not_reported():
    cfg = Config()
    assert Updater(cfg).apply_pushed_config({"log_level": cfg.log_level}) == []


# --- staging and applying ---------------------------------------------------
def stage_one(tmp_path, version="2.0.0"):
    cfg = Config()
    cfg.forward_url = "https://x/ingest"
    up = Updater(cfg, root=tmp_path)
    blob = io.BytesIO()
    with zipfile.ZipFile(blob, "w") as z:
        z.writestr("whoop_bridge/__init__.py", f'__version__ = "{version}"\n')
        z.writestr("whoop_bridge/newmod.py", "VALUE = 1\n")
    data = blob.getvalue()
    up._stage(data, {"version": version}, hashlib.sha256(data).hexdigest())
    return up


def test_apply_pending_installs_and_clears(tmp_path):
    stage_one(tmp_path)
    assert apply_pending(tmp_path) == "2.0.0"
    assert (tmp_path / "whoop_bridge" / "newmod.py").read_text() == "VALUE = 1\n"
    assert '"2.0.0"' in (tmp_path / "whoop_bridge" / "__init__.py").read_text()
    # Marker consumed, payload cleaned, install recorded.
    assert not (tmp_path / ".update" / "pending.json").exists()
    assert not (tmp_path / ".update" / "next").exists()
    assert json.loads((tmp_path / ".update" / "installed.json").read_text())["version"] == "2.0.0"


def test_apply_is_not_repeated_on_the_next_start(tmp_path):
    stage_one(tmp_path)
    assert apply_pending(tmp_path) == "2.0.0"
    assert apply_pending(tmp_path) is None


def test_apply_with_nothing_staged_is_a_no_op(tmp_path):
    assert apply_pending(tmp_path) is None


def test_corrupt_marker_is_discarded(tmp_path):
    stage = tmp_path / ".update"
    (stage / "next").mkdir(parents=True)
    (stage / "pending.json").write_text("{not json")
    assert apply_pending(tmp_path) is None
    assert not (stage / "pending.json").exists()


def test_restaging_the_same_digest_is_recognised(tmp_path):
    up = stage_one(tmp_path)
    digest = json.loads((tmp_path / ".update" / "pending.json").read_text())["sha256"]
    assert up._already_staged(digest) is True
    assert up._already_staged("something-else") is False


# --- server side ------------------------------------------------------------
def test_code_zip_is_deterministic():
    assert build_code_zip(ROOT) == build_code_zip(ROOT)


def test_code_zip_carries_no_credentials():
    names = zipfile.ZipFile(io.BytesIO(build_code_zip(ROOT))).namelist()
    assert not [n for n in names if "config.toml" in n or n.endswith((".db", ".env"))]


def test_release_digest_matches_the_archive():
    assert release(ROOT)["sha256"] == hashlib.sha256(build_code_zip(ROOT)).hexdigest()


def test_version_is_read_from_the_package():
    assert bridge_version(ROOT) == __import__("whoop_bridge").__version__


def test_server_config_ignores_unknown_and_malformed(monkeypatch):
    monkeypatch.setenv("BRIDGE_LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("BRIDGE_BATCH_SIZE", "not-a-number")
    monkeypatch.setenv("BRIDGE_INCLUDE_IMU", "true")
    out = pushed_config()
    assert out["log_level"] == "DEBUG" and out["include_imu"] is True
    assert "batch_size" not in out
