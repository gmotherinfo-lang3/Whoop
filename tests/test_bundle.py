"""The laptop bundle. It contains a live credential, so the guard around it
matters more than the zip itself."""
import io
import zipfile
from pathlib import Path

import pytest

from server.app.bundle import (
    build_zip, bundle_status, download_allowed, public_base_url, render_config,
)
from whoop_bridge.forwarder import is_private_host, is_transport_ok

ROOT = Path(__file__).resolve().parents[1]
CF = {"cf-connecting-ip": "203.0.113.9", "host": "whoop.example.com"}
CF_AUTHED = dict(CF, **{"cf-access-authenticated-user-email": "me@example.com"})


# --- the guard --------------------------------------------------------------
def test_cloudflare_request_needs_an_access_login(monkeypatch):
    monkeypatch.delenv("SETUP_DOWNLOAD", raising=False)
    allowed, reason = download_allowed(CF, "172.18.0.3")
    assert allowed is False and "Access" in reason


def test_cloudflare_request_with_access_is_allowed(monkeypatch):
    monkeypatch.delenv("SETUP_DOWNLOAD", raising=False)
    assert download_allowed(CF_AUTHED, "172.18.0.3")[0] is True


def test_direct_lan_client_is_allowed(monkeypatch):
    monkeypatch.delenv("SETUP_DOWNLOAD", raising=False)
    for ip in ("192.168.1.44", "10.0.0.5", "127.0.0.1", "172.16.4.4"):
        assert download_allowed({"host": "x"}, ip)[0] is True, ip


def test_direct_public_client_is_refused(monkeypatch):
    monkeypatch.delenv("SETUP_DOWNLOAD", raising=False)
    # A routable client with no Access login must never get the token.
    for ip in ("8.8.8.8", "93.184.216.34"):
        assert download_allowed({"host": "x"}, ip)[0] is False, ip


def test_unparseable_client_is_refused(monkeypatch):
    monkeypatch.delenv("SETUP_DOWNLOAD", raising=False)
    assert download_allowed({"host": "x"}, "not-an-ip")[0] is False
    assert download_allowed({"host": "x"}, None)[0] is False


def test_kill_switch_beats_everything(monkeypatch):
    monkeypatch.setenv("SETUP_DOWNLOAD", "off")
    assert download_allowed(CF_AUTHED, "192.168.1.5")[0] is False


def test_open_mode_bypasses_the_guard(monkeypatch):
    monkeypatch.setenv("SETUP_DOWNLOAD", "open")
    assert download_allowed({"host": "x"}, "8.8.8.8")[0] is True


# --- base URL ---------------------------------------------------------------
def test_base_url_prefers_forwarded_headers():
    assert public_base_url({"x-forwarded-host": "w.example.com",
                            "x-forwarded-proto": "https"}, "http://fb") == "https://w.example.com"


def test_base_url_assumes_https_through_the_tunnel():
    assert public_base_url(CF, "http://fb") == "https://whoop.example.com"


def test_base_url_is_http_for_a_direct_lan_hit():
    assert public_base_url({"host": "192.168.1.5:8000"}, "http://fb") == "http://192.168.1.5:8000"


def test_base_url_falls_back_when_no_host():
    assert public_base_url({}, "http://127.0.0.1:8000") == "http://127.0.0.1:8000"


# --- the zip ----------------------------------------------------------------
def test_bundle_contains_everything_the_laptop_needs():
    names = zipfile.ZipFile(io.BytesIO(build_zip("https://x", "t", root=ROOT))).namelist()
    assert "strap-laptop/config.toml" in names
    assert "strap-laptop/START-HERE.md" in names
    assert "strap-laptop/pyproject.toml" in names
    assert any(n.endswith("whoop_bridge/protocol.py") for n in names)
    assert any(n.endswith("tray/whoop_tray.py") for n in names)
    assert any(n.endswith("windows/setup.ps1") for n in names)


def test_bundle_excludes_build_junk_and_databases():
    names = zipfile.ZipFile(io.BytesIO(build_zip("https://x", "t", root=ROOT))).namelist()
    assert not [n for n in names if "__pycache__" in n or n.endswith((".pyc", ".db", ".log"))]


def test_generated_config_points_back_at_this_server():
    cfg = render_config("https://whoop.example.com", "secret-token")
    assert 'forward_url = "https://whoop.example.com/ingest"' in cfg
    assert 'forward_token = "secret-token"' in cfg
    assert 'address = ""' in cfg          # the one thing the user still supplies


def test_generated_config_can_carry_a_service_token():
    cfg = render_config("https://x", "t", "abc.access", "shh")
    assert 'cf_access_client_id = "abc.access"' in cfg
    assert 'cf_access_client_secret = "shh"' in cfg


def test_generated_config_is_loadable_by_the_bridge(tmp_path):
    from whoop_bridge.config import Config
    path = tmp_path / "config.toml"
    path.write_text(render_config("https://whoop.example.com", "tok"))
    cfg = Config.load(path)
    assert cfg.forward_url == "https://whoop.example.com/ingest"
    assert cfg.forward_token == "tok"
    # Only the strap address should be outstanding.
    assert cfg.validate() == ["no device address set (run `whoop-bridge scan` first)"]


def test_lan_generated_config_is_also_accepted(tmp_path):
    # Regression: the bundle generated http:// for LAN installs while the
    # forwarder refused anything but https, so that config could never run.
    from whoop_bridge.config import Config
    path = tmp_path / "config.toml"
    path.write_text(render_config("http://192.168.1.5:8000", "tok"))
    cfg = Config.load(path)
    cfg.address = "AA:BB:CC:DD:EE:FF"
    assert cfg.validate() == []


def test_bundle_status_reports_missing_files(tmp_path):
    assert bundle_status(ROOT)["ready"] is True
    assert bundle_status(tmp_path)["ready"] is False


# --- transport rule ---------------------------------------------------------
@pytest.mark.parametrize("url,ok", [
    ("https://whoop.example.com/ingest", True),
    ("http://192.168.1.5:8000/ingest", True),
    ("http://localhost:8000/ingest", True),
    ("http://whoop.lan:8000/ingest", True),
    ("http://example.com/ingest", False),     # public host over plain HTTP
    ("http://8.8.8.8/ingest", False),
    ("ftp://x/ingest", False),
])
def test_transport_rule(url, ok):
    assert is_transport_ok(url) is ok


def test_private_host_detection():
    assert is_private_host("10.1.2.3") and is_private_host("localhost")
    assert not is_private_host("example.com") and not is_private_host(None)
