"""Forwarder tests using a mock transport -- no real network involved."""
import asyncio
import json
import httpx
import pytest
from whoop_bridge.forwarder import Forwarder
from whoop_bridge.spool import Spool


async def drain(f, spool, stop_when_empty=True, limit=3.0):
    """Run the forwarder until the spool empties or `limit` seconds pass."""
    task = asyncio.create_task(f.run())
    async def watch():
        for _ in range(int(limit * 50)):
            if stop_when_empty and spool.depth() == 0:
                break
            await asyncio.sleep(0.02)
        f.stop()
    await asyncio.gather(watch(), task)


def test_rejects_plain_http(tmp_path):
    s = Spool(tmp_path / "s.db")
    with pytest.raises(ValueError, match="https"):
        Forwarder(s, url="http://insecure.test/ingest")
    s.close()


def test_successful_post_acks_and_empties_spool(tmp_path, monkeypatch):
    s = Spool(tmp_path / "s.db")
    for i in range(3):
        s.put({"received_at": "t", "heart_rate": 60 + i})
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200)

    f = Forwarder(s, url="https://example.test/ingest", interval=0.05, token="secret")
    _patch(monkeypatch, handler)
    asyncio.run(drain(f, s))
    assert s.depth() == 0
    assert len(seen[0]["records"]) == 3
    assert all("record_id" in r for r in seen[0]["records"])   # de-dup key present
    s.close()


def test_server_error_keeps_records_for_retry(tmp_path, monkeypatch):
    s = Spool(tmp_path / "s.db")
    s.put({"received_at": "t", "heart_rate": 60})
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        # Fail twice, then succeed -- the record must survive both failures.
        return httpx.Response(200 if calls["n"] > 2 else 503)

    f = Forwarder(s, url="https://example.test/ingest", interval=0.05, max_backoff=0.1)
    _patch(monkeypatch, handler)
    asyncio.run(drain(f, s, limit=5.0))
    assert calls["n"] >= 3
    assert s.depth() == 0        # at-least-once delivery held
    s.close()


def test_auth_and_signature_headers(tmp_path, monkeypatch):
    s = Spool(tmp_path / "s.db")
    s.put({"received_at": "t"})
    got = {}

    def handler(request: httpx.Request) -> httpx.Response:
        got.update(request.headers)
        return httpx.Response(200)

    f = Forwarder(s, url="https://example.test/ingest", interval=0.05,
                  token="tok123", hmac_secret="shh")
    _patch(monkeypatch, handler)
    asyncio.run(drain(f, s))
    assert got["authorization"] == "Bearer tok123"
    assert len(got["x-signature-sha256"]) == 64
    assert "idempotency-key" in got
    s.close()


def _patch(monkeypatch, handler):
    """Force httpx.AsyncClient to use the mock transport."""
    orig = httpx.AsyncClient.__init__
    def patched(self, *a, **kw):
        kw["transport"] = httpx.MockTransport(handler)
        orig(self, *a, **kw)
    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched)
