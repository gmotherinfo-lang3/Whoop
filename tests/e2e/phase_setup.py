"""Phases 0-2: a fresh server, the setup-bundle gate, and a bridge that runs
from nothing but the config it downloaded."""
from __future__ import annotations

import io
import json
import os
import shutil
import sys
import zipfile

sys.path.insert(0, os.path.dirname(__file__))
from harness import (INGEST_TOKEN, REPO, SERVICE_ID, SERVICE_SECRET, WORK, check,
                     http, jget, report, start_server, start_tunnel, wait_http)

PORT, TUNNEL_PORT = 8401, 8402
LAN = f"http://127.0.0.1:{PORT}"
TUNNEL = f"http://127.0.0.1:{TUNNEL_PORT}"
STATE = f"{WORK}/state"
DB = f"{STATE}/fresh.db"
LAPTOP = f"{STATE}/laptop"
SESSION = "CF_Authorization=valid-session"
SVC = {"CF-Access-Client-Id": SERVICE_ID, "CF-Access-Client-Secret": SERVICE_SECRET}

shutil.rmtree(STATE, ignore_errors=True)
os.makedirs(STATE, exist_ok=True)

srv = start_server(PORT, DB, log=f"{STATE}/server.log")
tun = start_tunnel(TUNNEL_PORT, LAN, log=f"{STATE}/tunnel.log")
try:
    assert wait_http(f"{LAN}/healthz"), "server never came up"
    assert wait_http(f"{TUNNEL}/healthz", cookie=SESSION), "tunnel never came up"

    # --- Phase 0: day zero. Nothing has ever been recorded. -----------------
    print("\n[phase 0] a brand-new install with an empty database")
    for path in ["/api/summary?days=7", "/api/health-monitor", "/api/stress",
                 "/api/fitness-age", "/api/advanced", "/api/insights?days=30",
                 "/api/advice", "/api/learning", "/api/device",
                 "/api/activities?date=2026-08-29", "/api/journal?days=30"]:
        status, body = jget(LAN + path)
        check(f"empty DB: {path} answers cleanly", status == 200,
              f"status={status} {str(body)[:120]}")

    status, _, _ = http(f"{LAN}/")
    check("empty DB: dashboard page loads", status == 200, f"status={status}")

    status, body = jget(f"{LAN}/api/stress")
    check("empty DB: stress explains itself rather than showing a number",
          body.get("usable") is False and bool(body.get("reason")),
          str(body)[:160])

    status, body = jget(f"{LAN}/api/fitness-age")
    check("empty DB: fitness age refuses without a resting heart rate",
          body.get("estimate", {}).get("usable") is False, str(body)[:160])

    # --- Phase 1: who may download the bundle ------------------------------
    print("\n[phase 1] the setup bundle hands out a live credential")
    status, body, _ = http(f"{LAN}/setup/bundle.zip")
    check("LAN request may download the bundle", status == 200, f"status={status}")
    lan_zip = body

    status, _, hdrs = http(f"{TUNNEL}/setup/bundle.zip")
    check("tunnel without Access is bounced at the edge", status == 302,
          f"status={status} location={hdrs.get('location','')[:60]}")

    status, body, _ = http(f"{TUNNEL}/setup/bundle.zip", headers=SVC)
    check("a service token cannot download the bundle", status == 403,
          f"status={status} {body[:120].decode(errors='replace')}")

    status, body, _ = http(f"{TUNNEL}/setup/bundle.zip", cookie=SESSION)
    check("a logged-in browser may download the bundle", status == 200, f"status={status}")
    tunnel_zip = body

    # --- Phase 2: the bundle is genuinely ready to run ---------------------
    print("\n[phase 2] the downloaded bundle is pre-configured")
    zf = zipfile.ZipFile(io.BytesIO(tunnel_zip))
    names = zf.namelist()
    check("bundle carries the bridge package",
          any(n.endswith("whoop_bridge/cli.py") for n in names), f"{len(names)} files")
    check("bundle carries the Windows installers",
          any("windows/setup.ps1" in n for n in names) and
          any("windows/install-task.ps1" in n for n in names))
    cfg_name = next((n for n in names if n.endswith("config.toml")), None)
    check("bundle carries a filled-in config.toml", cfg_name is not None)

    text = zf.read(cfg_name).decode()
    check("config points at the public hostname, not localhost",
          "whoop.example.com" in text, text[:200].replace("\n", " | "))
    check("config carries the ingest token", INGEST_TOKEN in text)
    check("config forwards to /ingest", "/ingest" in text)

    check("no compiled or database files were shipped",
          not [n for n in names if n.endswith((".pyc", ".db", ".db-wal", ".log"))])
    check("bundle ships no secrets file of its own",
          not [n for n in names if n.endswith((".env", "id_rsa", ".pem"))])

    # The LAN copy should point at the LAN, so a laptop set up on the LAN
    # keeps working when the tunnel is down.
    lan_cfg = zipfile.ZipFile(io.BytesIO(lan_zip)).read(cfg_name).decode()
    check("the LAN copy points at the LAN address",
          "127.0.0.1" in lan_cfg or "localhost" in lan_cfg,
          lan_cfg[:160].replace("\n", " | "))

    os.makedirs(LAPTOP, exist_ok=True)
    zf.extractall(LAPTOP)
    root = LAPTOP
    while len(os.listdir(root)) == 1 and os.path.isdir(os.path.join(root, os.listdir(root)[0])):
        root = os.path.join(root, os.listdir(root)[0])
    json.dump({"laptop_root": root, "config": os.path.join(root, "config.toml")},
              open(f"{STATE}/laptop.json", "w"))

    sys.path.insert(0, root)
    for mod in [m for m in list(sys.modules) if m.startswith("whoop_bridge")]:
        del sys.modules[mod]
    from whoop_bridge.config import Config
    cfg = Config.load(os.path.join(root, "config.toml"))
    check("the unpacked config loads with the bridge's own loader",
          cfg.forward_url.endswith("/ingest") and cfg.forward_token == INGEST_TOKEN,
          f"url={cfg.forward_url}")

    problems = cfg.validate()
    check("only the strap address is left to fill in",
          problems == ["no device address set (run `whoop-bridge scan` first)"],
          str(problems))
finally:
    tun.stop(); srv.stop()

sys.exit(report())
