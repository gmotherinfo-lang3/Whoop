"""Phase 9: one bad record must not stop the strap.

The bridge deletes a spooled row only after a 2xx and retries every 5xx
forever. So a record the server cannot store is not a lost record -- it is a
queue that never drains again, and a user whose data silently stops arriving
while the app still says the bridge is connected. This drives the whole real
path: spool, forwarder, server.
"""
from __future__ import annotations

import asyncio, json, os, shutil, sys, time
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from harness import (Client, WORK, check, jget, report, start_server,
                     wait_http)

import strap                                            # noqa: E402
from whoop_bridge.decode import decode                  # noqa: E402
from whoop_bridge.forwarder import Forwarder            # noqa: E402
from whoop_bridge.protocol import parse_frame           # noqa: E402
from whoop_bridge.spool import Spool                    # noqa: E402

PORT = 8461; LAN = f"http://127.0.0.1:{PORT}"
STATE = f"{WORK}/state8"
shutil.rmtree(STATE, ignore_errors=True); os.makedirs(STATE)
srv = start_server(PORT, f"{STATE}/whoop.db", log=f"{STATE}/server.log")

try:
    assert wait_http(f"{LAN}/healthz")
    owner = Client(LAN)
    owner.sign_up_owner()
    token = owner.pair_a_laptop()
    spool = Spool(f"{STATE}/spool.db")
    now = int(time.time())

    good_before, good_after = 40, 40
    ids = []
    for i in range(good_before):
        rec = decode(parse_frame(strap.historical(now - (200 - i) * 60, 70 + i % 20,
                                                  [840, 855, 830, 848])), "data")
        ids.append(rec["record_id"]); spool.put(rec)

    # The kind of record a firmware variant or a half-decoded frame produces.
    poison = decode(parse_frame(strap.historical(now - 150 * 60, 71,
                                                 [840, 850, 830, 845])), "data")
    poison["heart_rate"] = {"unexpected": "shape"}
    poison["gravity_x"] = ["also", "wrong"]
    spool.put(poison)

    for i in range(good_after):
        rec = decode(parse_frame(strap.historical(now - (100 - i) * 60, 66 + i % 20,
                                                  [900, 915, 890, 905])), "data")
        ids.append(rec["record_id"]); spool.put(rec)

    depth0 = spool.depth()
    check("the spool is holding a poisoned record among good ones",
          depth0 == good_before + good_after + 1, f"depth={depth0}")

    async def drain():
        fw = Forwarder(spool, url=f"{LAN}/ingest", token=token,
                       batch_size=50, interval=0.5)
        task = asyncio.create_task(fw.run())
        for _ in range(60):                  # 30 seconds is generous
            if spool.depth() == 0:
                break
            await asyncio.sleep(0.5)
        fw.stop()
        await asyncio.wait_for(task, timeout=15)

    t0 = time.time()
    asyncio.run(drain())
    took = time.time() - t0

    check("the spool drained instead of wedging", spool.depth() == 0,
          f"depth={spool.depth()} after {took:.0f}s")

    import sqlite3
    con = sqlite3.connect(f"file:{STATE}/data-1.db?mode=ro", uri=True)
    stored = {r[0] for r in con.execute("SELECT record_id FROM records")}
    bad = con.execute("SELECT heart_rate, gravity_x FROM records WHERE record_id = ?",
                      (poison["record_id"],)).fetchone()
    con.close()

    check("every good record arrived", set(ids) <= stored,
          f"missing {len(set(ids) - stored)}")
    check("the records after the bad one arrived too",
          len(stored & set(ids[good_before:])) == good_after)
    check("the poisoned record is kept, with the unusable fields nulled",
          bad is not None and bad[0] is None and bad[1] is None, str(bad))

    time.sleep(4)                       # past the ingest coalescing window
    _, day = owner.call("/api/summary?days=2")
    check("the day still computes with the poisoned record in it",
          any(d.get("has_data") for d in day.get("days", [])), str(day)[:160])
    _, hm = owner.call("/api/health-monitor")
    check("the health monitor reads the day without tripping over it",
          hm.get("heart_rate", {}).get("latest") is not None, str(hm)[:160])
finally:
    srv.stop()

sys.exit(report())
