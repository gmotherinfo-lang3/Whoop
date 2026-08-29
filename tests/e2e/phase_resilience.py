"""Phase 10: the awkward days.

The laptop reboots. The tunnel falls over while you are on the sofa with the
laptop. You and your phone both save the journal in the same second. The strap
comes off your wrist. None of it should lose data or mislead you.
"""
from __future__ import annotations

import asyncio, json, os, shutil, sqlite3, sys, threading, time
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from harness import (INGEST_TOKEN, Proc, SERVICE_ID, SERVICE_SECRET, WORK, check,
                     http, jget, report, start_server, start_tunnel, wait_http)

import strap                                            # noqa: E402
from whoop_bridge.decode import decode                  # noqa: E402
from whoop_bridge.forwarder import Forwarder            # noqa: E402
from whoop_bridge.protocol import parse_frame           # noqa: E402
from whoop_bridge.spool import Spool                    # noqa: E402

PORT, TUNNEL_PORT = 8471, 8472
LAN, TUNNEL = f"http://127.0.0.1:{PORT}", f"http://127.0.0.1:{TUNNEL_PORT}"
STATE = f"{WORK}/state9"
SESSION = "CF_Authorization=valid-session"
SVC = {"CF-Access-Client-Id": SERVICE_ID, "CF-Access-Client-Secret": SERVICE_SECRET}
TODAY = time.strftime("%Y-%m-%d", time.gmtime())
JSON_H = {"Content-Type": "application/json"}

shutil.rmtree(STATE, ignore_errors=True); os.makedirs(STATE)
srv = start_server(PORT, f"{STATE}/whoop.db", log=f"{STATE}/server.log")
tun = start_tunnel(TUNNEL_PORT, LAN, log=f"{STATE}/tunnel.log")

try:
    assert wait_http(f"{LAN}/healthz")
    assert wait_http(f"{TUNNEL}/healthz", cookie=SESSION)
    now = int(time.time())

    # --- the laptop reboots with records still queued ----------------------
    print("\n[phase 10a] the laptop reboots before the queue has drained")
    spool_path = f"{STATE}/spool.db"
    spool = Spool(spool_path)
    ids = []
    for i in range(120):
        r = decode(parse_frame(strap.historical(now - (300 - i) * 60, 68 + i % 15,
                                                [870, 885, 860, 878])), "data")
        ids.append(r["record_id"]); spool.put(r)
    check("records are queued on disk", spool.depth() == 120, f"depth={spool.depth()}")
    spool.close()                                    # the laptop powers off

    reopened = Spool(spool_path)                     # and powers back on
    check("the queue survived the reboot", reopened.depth() == 120,
          f"depth={reopened.depth()}")

    async def drain(sp):
        fw = Forwarder(sp, url=f"{TUNNEL}/ingest", token=INGEST_TOKEN, batch_size=50,
                       interval=0.4, cf_access_client_id=SERVICE_ID,
                       cf_access_client_secret=SERVICE_SECRET)
        t = asyncio.create_task(fw.run())
        for _ in range(60):
            if sp.depth() == 0:
                break
            await asyncio.sleep(0.5)
        fw.stop(); await asyncio.wait_for(t, timeout=15)

    asyncio.run(drain(reopened))
    con = sqlite3.connect(f"file:{STATE}/whoop.db?mode=ro", uri=True)
    stored = {r[0] for r in con.execute("SELECT record_id FROM records")}
    con.close()
    check("everything queued before the reboot was delivered afterwards",
          set(ids) <= stored, f"missing {len(set(ids) - stored)}")

    # --- what the phone is told about the bridge ---------------------------
    print("\n[phase 10b] the phone can see whether the bridge is actually alive")
    status, dev = jget(f"{TUNNEL}/api/device", cookie=SESSION)
    check("before any heartbeat the phone is told there is no bridge yet",
          status == 200 and dev.get("state") == "unknown", str(dev)[:140])

    beat = {"connected": True, "address": "AA:BB:CC:DD:EE:FF", "battery_pct": 71.0,
            "on_wrist": True, "spool_depth": 0, "last_record_at": time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    status, _ = jget(f"{TUNNEL}/status", method="POST", data=json.dumps(beat).encode(),
                     headers={**SVC, **JSON_H, "Authorization": f"Bearer {INGEST_TOKEN}"})
    check("the bridge's heartbeat is accepted through the tunnel", status == 200,
          f"status={status}")

    status, dev = jget(f"{TUNNEL}/api/device", cookie=SESSION)
    check("the phone now sees the strap's battery and state",
          status == 200 and dev.get("state") != "unknown" and dev.get("battery_pct") == 71.0,
          str(dev)[:180])

    stale = dict(beat, connected=False, last_record_at="2020-01-01T00:00:00Z")
    jget(f"{TUNNEL}/status", method="POST", data=json.dumps(stale).encode(),
         headers={**SVC, **JSON_H, "Authorization": f"Bearer {INGEST_TOKEN}"})
    status, dev = jget(f"{LAN}/api/device")
    check("a disconnected bridge is reported as disconnected, not as silence",
          dev.get("state") in ("disconnected", "stale", "offline") or
          "connect" in (dev.get("label", "") + dev.get("detail", "")).lower(),
          str(dev)[:200])

    # --- the tunnel dies; the laptop must not care -------------------------
    print("\n[phase 10c] the tunnel falls over")
    tun.stop()
    check("the phone can no longer reach the app",
          http(f"{TUNNEL}/healthz", timeout=3, cookie=SESSION)[0] == 0)
    check("the laptop on the LAN is unaffected", http(f"{LAN}/healthz")[0] == 200)
    status, _ = jget(f"{LAN}/api/journal/{TODAY}", method="PUT",
                     data=json.dumps({"tags": ["nap"], "amounts": {},
                                      "notes": "written on the laptop"}).encode(),
                     headers=JSON_H)
    check("the laptop can still write with the tunnel down", status == 200,
          f"status={status}")
    status, _, _ = http(f"{LAN}/setup/bundle.zip")
    check("the LAN setup download still works with the tunnel down", status == 200)

    tun2 = start_tunnel(TUNNEL_PORT, LAN, log=f"{STATE}/tunnel2.log")
    assert wait_http(f"{TUNNEL}/healthz", cookie=SESSION), "tunnel did not come back"
    status, entry = jget(f"{TUNNEL}/api/journal/{TODAY}", cookie=SESSION)
    check("when the tunnel returns the phone sees what the laptop wrote",
          entry.get("notes") == "written on the laptop", str(entry)[:140])

    # --- both of you saving at once ----------------------------------------
    print("\n[phase 10d] the laptop and the phone save at the same moment")
    results, errors = [], []

    def writer(label, url, cookie, n):
        for i in range(n):
            st, _ = jget(f"{url}/api/journal/{TODAY}", method="PUT", cookie=cookie,
                         data=json.dumps({"tags": [label], "amounts": {},
                                          "notes": f"{label} {i}"}).encode(),
                         headers=JSON_H)
            (results if st == 200 else errors).append((label, st))

    threads = [threading.Thread(target=writer, args=("laptop", LAN, None, 25)),
               threading.Thread(target=writer, args=("phone", TUNNEL, SESSION, 25))]
    for t in threads: t.start()
    for t in threads: t.join(timeout=90)
    check("every concurrent journal write succeeded", not errors,
          f"{len(errors)} failed: {errors[:3]}")
    status, entry = jget(f"{LAN}/api/journal/{TODAY}")
    check("the entry is one of the writes, not a mangled mixture",
          status == 200 and len(entry.get("tags", [])) == 1 and
          entry["tags"][0] in ("laptop", "phone") and
          entry["notes"].startswith(entry["tags"][0]),
          str(entry)[:160])

    # --- writing while the strap streams -----------------------------------
    print("\n[phase 10e] writing from the phone while records pour in")
    stop = threading.Event()
    ingested = {"n": 0, "fail": 0}

    def stream():
        i = 0
        batches = 0
        while not stop.is_set() or batches < 25:
            batch = []
            for _ in range(20):
                i += 1
                batch.append(decode(parse_frame(strap.historical(
                    now + i, 70 + i % 25, [850, 865, 840, 858])), "data"))
            st, body = jget(f"{LAN}/ingest", method="POST",
                            data=json.dumps({"records": batch}).encode(),
                            headers={**JSON_H, "Authorization": f"Bearer {INGEST_TOKEN}"})
            if st == 200: ingested["n"] += body.get("inserted", 0)
            else: ingested["fail"] += 1
            batches += 1
            time.sleep(0.05)

    t = threading.Thread(target=stream); t.start()
    write_fail = []
    for i in range(20):
        st, _ = jget(f"{TUNNEL}/api/intake", method="POST", cookie=SESSION,
                     data=json.dumps({"at": f"{TODAY}T{9 + i % 12:02d}:00:00Z",
                                      "substance": "caffeine", "amount": 80,
                                      "label": "coffee"}).encode(), headers=JSON_H)
        if st != 200: write_fail.append(st)
        st, _ = jget(f"{TUNNEL}/api/journal/{TODAY}", method="PUT", cookie=SESSION,
                     data=json.dumps({"tags": ["caffeine"], "amounts": {},
                                      "notes": f"note {i}"}).encode(), headers=JSON_H)
        if st != 200: write_fail.append(st)
    stop.set(); t.join(timeout=90)

    check("the phone's writes all succeeded under a heavy ingest load",
          not write_fail, f"{len(write_fail)} failed: {write_fail[:4]}")
    check("ingest kept succeeding at the same time",
          ingested["fail"] == 0 and ingested["n"] >= 500,
          f"inserted={ingested['n']} failed_batches={ingested['fail']}")
    status, intake = jget(f"{LAN}/api/intake?date={TODAY}")
    check("every intake entry the phone wrote is there",
          len(intake.get("entries", [])) == 20, f"{len(intake.get('entries', []))} of 20")

    time.sleep(4)
    for path in ["/api/summary?days=3", "/api/health-monitor", "/api/stress",
                 "/api/fitness-age", "/api/advanced", "/api/advice"]:
        st, _ = jget(LAN + path)
        check(f"after all of that, {path} still answers", st == 200, f"status={st}")
    tun2.stop()
finally:
    for p in (tun, srv):
        try: p.stop()
        except Exception: pass

sys.exit(report())
