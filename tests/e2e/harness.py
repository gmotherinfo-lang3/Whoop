"""Shared plumbing for the end-to-end runs: processes, HTTP, reporting."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = str(Path(__file__).resolve().parents[2])
# Where the phases put their databases, spools, logs and unpacked bundles.
WORK = os.environ.get("E2E_WORK", str(Path(__file__).resolve().parent))
# The browser the UI phases drive. Any Chromium Playwright can launch will do.
CHROME = os.environ.get("E2E_CHROME") or next(
    (str(p) for p in sorted(Path("/opt/pw-browsers").glob("chromium*/chrome-linux/chrome"))),
    "")
INGEST_TOKEN = "e2e-ingest-token-9f3a"
SERVICE_ID = "svc.id"
SERVICE_SECRET = "svc.secret"

_results: list[tuple[bool, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    _results.append((bool(ok), name, detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  -- {detail}" if detail else ""),
          flush=True)
    return bool(ok)


def report() -> int:
    failed = [r for r in _results if not r[0]]
    print(f"\n{'='*72}\n{len(_results) - len(failed)}/{len(_results)} checks passed")
    for _, name, detail in failed:
        print(f"  FAILED: {name}  {detail}")
    return 1 if failed else 0


class Proc:
    """A child process that is reliably cleaned up."""

    def __init__(self, argv, env=None, cwd=None, log=None):
        self.log_path = log
        self.fh = open(log, "wb") if log else subprocess.DEVNULL
        self.p = subprocess.Popen(argv, env={**os.environ, **(env or {})}, cwd=cwd,
                                  stdout=self.fh, stderr=subprocess.STDOUT,
                                  start_new_session=True)

    def stop(self, sig=signal.SIGTERM):
        if self.p.poll() is None:
            try:
                os.killpg(os.getpgid(self.p.pid), sig)
            except ProcessLookupError:
                pass
            try:
                self.p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(os.getpgid(self.p.pid), signal.SIGKILL)
                self.p.wait(timeout=5)
        if self.fh is not subprocess.DEVNULL:
            self.fh.close()

    def tail(self, n=25):
        if not self.log_path or not os.path.exists(self.log_path):
            return ""
        return "\n".join(open(self.log_path, errors="replace").read().splitlines()[-n:])


def http(url, *, method="GET", data=None, headers=None, timeout=30, cookie=None):
    """Returns (status, body_bytes, headers) and never raises on an HTTP error."""
    h = dict(headers or {})
    if cookie:
        h["Cookie"] = cookie
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    opener = urllib.request.build_opener(NoRedirect)
    try:
        with opener.open(req, timeout=timeout) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)
    except Exception as e:                       # connection refused, timeout
        return 0, str(e).encode(), {}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **kw):
        return None


def jget(url, **kw):
    status, body, _ = http(url, **kw)
    try:
        return status, json.loads(body)
    except Exception:
        return status, {"_raw": body[:400].decode(errors="replace")}


def wait_http(url, want=200, tries=40, delay=0.5, **kw):
    for _ in range(tries):
        status, _, _ = http(url, timeout=5, **kw)
        if status == want:
            return True
        time.sleep(delay)
    return False


def start_server(port, db, *, extra_env=None, log=None):
    env = {"WHOOP_DB": db, "INGEST_TOKEN": INGEST_TOKEN, "MAX_HR": "190",
           "USER_AGE": "34", "USER_SEX": "male", "TZ_OFFSET_HOURS": "0",
           "PYTHONUNBUFFERED": "1", **(extra_env or {})}
    p = Proc([sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
              "--port", str(port), "--log-level", "info"],
             env=env, cwd=f"{REPO}/server", log=log)
    return p


def start_tunnel(port, origin, *, log=None):
    env = {"TUNNEL_ORIGIN": origin, "ACCESS_SERVICE_ID": SERVICE_ID,
           "ACCESS_SERVICE_SECRET": SERVICE_SECRET, "PYTHONUNBUFFERED": "1"}
    return Proc([sys.executable, "-m", "uvicorn", "tunnel:app", "--host", "127.0.0.1",
                 "--port", str(port), "--log-level", "warning"],
                env=env, cwd=WORK, log=log)
