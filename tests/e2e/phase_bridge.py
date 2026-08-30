"""Phases 3-4: the bridge delivers through the tunnel, and survives an outage."""
from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import time
import zipfile

sys.path.insert(0, os.path.dirname(__file__))
from harness import (Client, INGEST_TOKEN, OWNER_EMAIL, Proc, SERVICE_ID,
                     SERVICE_SECRET, WORK, check, http, jget, report,
                     start_server, start_tunnel, wait_http)

PORT, TUNNEL_PORT = 8411, 8412
LAN, TUNNEL = f"http://127.0.0.1:{PORT}", f"http://127.0.0.1:{TUNNEL_PORT}"
STATE = f"{WORK}/state2"
DB = f"{STATE}/whoop.db"
SESSION = "CF_Authorization=valid-session"
SVC = {"CF-Access-Client-Id": SERVICE_ID, "CF-Access-Client-Secret": SERVICE_SECRET}

shutil.rmtree(STATE, ignore_errors=True)
os.makedirs(f"{STATE}/laptop", exist_ok=True)

srv = start_server(PORT, DB, log=f"{STATE}/server.log")
tun = start_tunnel(TUNNEL_PORT, LAN, log=f"{STATE}/tunnel.log")


DATA_DB = f"{STATE}/data-1.db"


def records_total() -> int:
    import sqlite3
    con = sqlite3.connect(f"file:{DATA_DB}?mode=ro", uri=True)
    try:
        return con.execute("SELECT COUNT(*) FROM records").fetchone()[0]
    finally:
        con.close()


try:
    assert wait_http(f"{LAN}/healthz"), "server down"
    assert wait_http(f"{TUNNEL}/healthz", cookie=SESSION), "tunnel down"

    owner = Client(LAN)
    owner.sign_up_owner()

    # --- the bundle, this time with a service token so the bridge can post ---
    print("\n[phase 3] the bridge posts through the tunnel with a service token")
    body = f"cf_access_client_id={SERVICE_ID}&cf_access_client_secret={SERVICE_SECRET}".encode()
    status, zdata, _ = http(f"{TUNNEL}/setup/bundle.zip", method="POST", data=body,
                            cookie=SESSION,
                            headers={"Content-Type": "application/x-www-form-urlencoded"})
    check("bundle downloads with the service token filled in", status == 200, f"status={status}")
    zf = zipfile.ZipFile(io.BytesIO(zdata))
    zf.extractall(f"{STATE}/laptop")
    root = f"{STATE}/laptop"
    while len(os.listdir(root)) == 1 and os.path.isdir(f"{root}/{os.listdir(root)[0]}"):
        root = f"{root}/{os.listdir(root)[0]}"
    cfg_path = f"{root}/config.toml"
    text = open(cfg_path).read()
    check("the config carries the service token so the edge lets the bridge in",
          SERVICE_ID in text and SERVICE_SECRET in text)
    check("but no key to post with -- that comes from pairing",
          'forward_token = ""' in text)

    # The laptop claims its own key, exactly as `whoop-bridge pair` does.
    device_token = owner.pair_a_laptop("e2e laptop")
    lines = []
    for line in text.splitlines():
        if line.strip().startswith("forward_token"):
            line = f'forward_token = "{device_token}"'
        lines.append(line)
    open(cfg_path, "w").write("\n".join(lines))
    check("the paired key is now in the config", device_token in open(cfg_path).read())

    # The bundle points at https://whoop.example.com; in this container that
    # name does not resolve, so the bridge is pointed at the tunnel's address.
    # Everything else -- token, service token, headers -- is what it downloaded.
    stats = f"{STATE}/run1.json"
    lap = Proc([sys.executable, f"{WORK}/laptop.py", "--config", cfg_path,
                "--spool", f"{STATE}/spool.db", "--url", f"{TUNNEL}/ingest",
                "--backfill-minutes", "1440", "--stats", stats],
               log=f"{STATE}/laptop1.log")
    lap.p.wait(timeout=300)
    produced = json.load(open(stats))
    check("the bridge decoded and spooled a day of records",
          produced["produced"] == 1440, str(produced["produced"]))
    check("the spool drained completely", produced["spool_depth"] == 0,
          f"depth={produced['spool_depth']}")
    check("every record arrived at the server", records_total() == 1440,
          f"server has {records_total()}")

    _, day = owner.call("/api/summary?days=2")
    have = [d for d in day.get("days", []) if d.get("has_data")]
    check("the server computed a day from what the bridge sent", bool(have),
          str(day)[:200])

    # --- Phase 4: the server goes away mid-stream --------------------------
    print("\n[phase 4] the server goes away while the strap is connected")
    before = records_total()
    lap2 = Proc([sys.executable, f"{WORK}/laptop.py", "--config", cfg_path,
                 "--spool", f"{STATE}/spool.db", "--url", f"{TUNNEL}/ingest",
                 "--live-seconds", "22", "--live-rate", "8",
                 "--stats", f"{STATE}/run2.json"], log=f"{STATE}/laptop2.log")
    time.sleep(5)
    srv.stop()
    check("server is really down", http(f"{LAN}/healthz", timeout=3)[0] == 0)
    outage_start = records_total()
    time.sleep(9)

    srv = start_server(PORT, DB, log=f"{STATE}/server2.log")
    assert wait_http(f"{LAN}/healthz"), "server did not come back"
    owner.sign_in()
    print("  server back up; waiting for the spool to drain")
    lap2.p.wait(timeout=300)
    produced2 = json.load(open(f"{STATE}/run2.json"))

    check("the bridge kept collecting through the outage",
          produced2["produced"] > 80, f"produced={produced2['produced']}")
    check("the spool drained once the server returned",
          produced2["spool_depth"] == 0, f"depth={produced2['spool_depth']}")

    delivered = records_total() - before
    check("nothing collected during the outage was lost",
          delivered == produced2["produced"],
          f"delivered={delivered} produced={produced2['produced']}")

    import sqlite3
    con = sqlite3.connect(f"file:{DATA_DB}?mode=ro", uri=True)
    dupes = con.execute("SELECT COUNT(*) FROM (SELECT record_id FROM records "
                        "GROUP BY record_id HAVING COUNT(*) > 1)").fetchone()[0]
    con.close()
    check("retries did not duplicate anything", dupes == 0, f"{dupes} duplicated ids")

    # --- replaying an old batch must be a no-op ----------------------------
    print("\n[phase 4b] a replayed batch is absorbed, not double-counted")
    ids = produced["ids"][:50]
    con = sqlite3.connect(f"file:{DATA_DB}?mode=ro", uri=True)
    rows = con.execute("SELECT record_id, device_unix, heart_rate FROM records "
                       f"WHERE record_id IN ({','.join('?' * len(ids))})", ids).fetchall()
    con.close()
    replay = [{"record_id": r[0], "unix": r[1], "heart_rate": r[2],
               "packet": "HISTORICAL_DATA", "version": 24} for r in rows]
    before_replay = records_total()
    status, body = jget(f"{TUNNEL}/ingest", method="POST",
                        data=json.dumps({"records": replay}).encode(),
                        headers={**SVC, "Content-Type": "application/json",
                                 "Authorization": f"Bearer {device_token}"})
    check("a replayed batch is accepted", status == 200, f"status={status} {body}")
    check("a replayed batch inserts nothing new",
          records_total() == before_replay and body.get("duplicates") == len(replay),
          f"inserted={body.get('inserted')} duplicates={body.get('duplicates')}")

    # --- auth at the edge and at the app -----------------------------------
    print("\n[phase 4c] the ways in that must stay shut")
    status, _, _ = http(f"{TUNNEL}/ingest", method="POST", data=b'{"records":[]}',
                        headers={"Content-Type": "application/json",
                                 "Authorization": f"Bearer {device_token}"})
    check("a valid device key alone does not get past Access", status == 302,
          f"status={status}")

    status, _, _ = http(f"{TUNNEL}/ingest", method="POST", data=b'{"records":[]}',
                        headers={**SVC, "Content-Type": "application/json",
                                 "Authorization": "Bearer not-a-real-device-key"})
    check("a valid service token with an unknown device key is refused",
          status == 401, f"status={status}")

    status, _, _ = http(f"{TUNNEL}/ingest", method="POST", data=b'{"records":[]}',
                        headers={"CF-Access-Client-Id": SERVICE_ID,
                                 "CF-Access-Client-Secret": "wrong",
                                 "Content-Type": "application/json",
                                 "Authorization": f"Bearer {INGEST_TOKEN}"})
    check("a wrong service token is bounced at the edge", status == 302, f"status={status}")

    # --- the bridge update channel -----------------------------------------
    print("\n[phase 5] the bridge can update itself through the tunnel")
    hdr = {**SVC, "Authorization": f"Bearer {device_token}"}
    status, rel = jget(f"{TUNNEL}/api/bridge/release", headers=hdr)
    check("release metadata is served through the tunnel",
          status == 200 and rel.get("version") and rel.get("sha256"), str(rel)[:160])
    status, zbody, zh = http(f"{TUNNEL}/api/bridge/bundle.zip", headers=hdr)
    import hashlib
    check("the update zip matches its advertised digest",
          status == 200 and hashlib.sha256(zbody).hexdigest() == rel.get("sha256"),
          f"status={status} len={len(zbody)}")
    status, _, _ = http(f"{TUNNEL}/api/bridge/bundle.zip", headers=SVC)
    check("the update zip still needs a paired key", status == 401, f"status={status}")
finally:
    for p in (tun, srv):
        try: p.stop()
        except Exception: pass

sys.exit(report())
