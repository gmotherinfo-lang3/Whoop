"""Phase 7: the inputs a real strap and a real network actually produce.

A strap with a lost RTC emits 1970 timestamps. A laptop that slept emits a
burst out of order. A flaky BLE link emits half-frames. None of these should
corrupt a day or take the server down.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from harness import INGEST_TOKEN, WORK, check, http, jget, report, start_server, wait_http

PORT = 8431
LAN = f"http://127.0.0.1:{PORT}"
STATE = f"{WORK}/state4"
DB = f"{STATE}/whoop.db"
AUTH = {"Authorization": f"Bearer {INGEST_TOKEN}", "Content-Type": "application/json"}
TODAY = time.strftime("%Y-%m-%d", time.gmtime())

shutil.rmtree(STATE, ignore_errors=True)
os.makedirs(STATE, exist_ok=True)
srv = start_server(PORT, DB, log=f"{STATE}/server.log")


def post(records):
    return jget(f"{LAN}/ingest", method="POST",
                data=json.dumps({"records": records}).encode(), headers=AUTH)


def alive():
    return http(f"{LAN}/healthz", timeout=5)[0] == 200


def rec(unix, hr=72, **kw):
    import hashlib
    base = {"record_id": hashlib.sha256(f"{unix}:{hr}:{kw}".encode()).hexdigest()[:32],
            "packet": "HISTORICAL_DATA", "version": 24, "unix": unix,
            "heart_rate": hr, "rr_intervals_ms": [830, 845, 820, 838],
            "gravity_x": 0.1, "gravity_y": 0.9, "gravity_z": 0.1, "skin_contact": 1}
    base.update(kw)
    return base


try:
    assert wait_http(f"{LAN}/healthz"), "server down"
    now = int(time.time())

    print("\n[phase 7] hostile and malformed input")

    # A strap whose real-time clock was lost reports 1970.
    status, body = post([rec(0, 70), rec(1, 71), rec(86400, 72)])
    check("records from a strap with a lost clock are accepted, not fatal",
          status == 200, f"status={status} {body}")
    check("server still healthy after 1970 timestamps", alive())
    status, body = jget(f"{LAN}/api/summary?days=2")
    check("a lost-clock day does not corrupt today's summary",
          status == 200 and isinstance(body.get("days"), list), str(body)[:140])

    # A clock running ahead.
    status, body = post([rec(now + 400 * 86400, 70), rec(now + 4_000_000_000, 71)])
    check("far-future timestamps are accepted without blowing up",
          status == 200, f"status={status} {body}")
    check("server still healthy after future timestamps", alive())

    # Out-of-order arrival, as after a laptop wakes from sleep.
    batch = [rec(now - i * 60, 60 + (i % 30)) for i in range(300, 0, -1)]
    shuffled = batch[150:] + batch[:150]
    status, body = post(shuffled)
    check("an out-of-order burst is accepted whole",
          status == 200 and body.get("inserted") == 300, str(body))

    time.sleep(4)   # past the ingest coalescing window
    status, day = jget(f"{LAN}/api/day/{TODAY}")
    check("the day computes normally from out-of-order records",
          status == 200 and (day.get("heart_rate") or {}).get("avg"), str(day)[:160])

    # Records missing everything that matters.
    status, body = post([{"record_id": "onlyanid"}, {"unix": now},
                         {"record_id": "x1", "unix": None, "heart_rate": None}])
    check("records missing fields are absorbed rather than rejected",
          status == 200, f"status={status} {body}")
    check("a record with no id is not stored", body.get("inserted", 0) <= 2, str(body))

    # Wrong types where numbers belong.
    status, body = post([{"record_id": "bad1", "unix": "not-a-number", "heart_rate": "fast"},
                         {"record_id": "bad2", "unix": now, "heart_rate": {"nested": 1}},
                         {"record_id": "bad3", "unix": now, "rr_intervals_ms": "830"}])
    check("wrong types do not take the endpoint down", status in (200, 422),
          f"status={status} {str(body)[:140]}")
    check("a batch containing one unbindable value still stores the good rows",
          status != 200 or body.get("inserted", 0) >= 2, str(body))
    check("server still healthy after wrong types", alive())

    # Integers outside the 64 bits SQLite stores. These raise OverflowError,
    # which is not an sqlite3 error, so they take a separate path to the same
    # stalled queue if it is not handled.
    status, body = post([{"record_id": "huge1", "unix": 2 ** 100, "heart_rate": 70},
                         {"record_id": "huge2", "unix": now, "heart_rate": 10 ** 40},
                         {"record_id": "huge3", "unix": -(2 ** 100), "heart_rate": 70}])
    check("integers too big for the database do not take the endpoint down",
          status == 200, f"status={status} {str(body)[:140]}")
    check("server still healthy after out-of-range integers", alive())

    # Absurd values that would break an average.
    status, body = post([rec(now - 5000, 100000), rec(now - 5060, -40),
                         rec(now - 5120, 72, rr_intervals_ms=[0, 0, 0]),
                         rec(now - 5180, 72, rr_intervals_ms=[999999, 1])])
    check("absurd values are accepted", status == 200, f"status={status}")
    status, day = jget(f"{LAN}/api/day/{TODAY}")
    hrv = (day.get("hrv") or {}).get("rmssd_ms")
    check("a zero-RR record does not produce a fake HRV of 0",
          hrv is None or hrv > 0, f"rmssd={hrv}")
    check("every endpoint still answers after absurd values",
          all(jget(LAN + p)[0] == 200 for p in
              ["/api/health-monitor", "/api/stress", "/api/fitness-age",
               "/api/advanced", "/api/advice"]))

    # An empty batch, and a very large one.
    status, body = post([])
    check("an empty batch is a no-op", status == 200 and body.get("inserted") == 0, str(body))

    big = [rec(now - 200000 - i * 60, 60 + (i % 40)) for i in range(5000)]
    t0 = time.time()
    status, body = post(big)
    took = time.time() - t0
    check("a 5000-record catch-up batch is accepted",
          status == 200 and body.get("inserted") == 5000, f"{body} in {took:.1f}s")
    check("a large catch-up batch completes promptly", took < 30, f"{took:.1f}s")

    # Malformed JSON and oversized junk.
    status, _, _ = http(f"{LAN}/ingest", method="POST", data=b"{not json", headers=AUTH)
    check("malformed JSON is rejected cleanly", status in (400, 422), f"status={status}")
    status, _, _ = http(f"{LAN}/ingest", method="POST",
                        data=json.dumps({"records": "a string"}).encode(), headers=AUTH)
    check("a wrong-shaped body is rejected cleanly", status == 422, f"status={status}")
    check("server still healthy after malformed bodies", alive())

    # Unparsed frames, which the bridge really does spool on a bad BLE link.
    status, body = post([{"record_id": f"unparsed{i}", "source": "data",
                          "kind": "unparsed", "raw_hex": "aa10ff" + "00" * 20}
                         for i in range(20)])
    check("unparsed frames are stored without breaking the day", status == 200, str(body))
    check("the day still computes with unparsed frames present",
          jget(f"{LAN}/api/day/{TODAY}")[0] == 200)

    # --- the write endpoints, from a phone with fat fingers ----------------
    print("\n[phase 7b] bad writes from the app itself")
    status, _ = jget(f"{LAN}/api/journal/not-a-date", method="PUT",
                     data=json.dumps({"tags": [], "amounts": {}, "notes": ""}).encode(),
                     headers={"Content-Type": "application/json"})
    check("a malformed journal date is refused", status in (400, 422), f"status={status}")

    status, _ = jget(f"{LAN}/api/activities", method="POST",
                     data=json.dumps({"start_unix": now, "end_unix": now - 3600,
                                      "activity_type": "run"}).encode(),
                     headers={"Content-Type": "application/json"})
    check("an activity that ends before it starts is refused",
          status in (400, 422), f"status={status}")

    status, _ = jget(f"{LAN}/api/activities/999999", method="PATCH",
                     data=json.dumps({"note": "nope"}).encode(),
                     headers={"Content-Type": "application/json"})
    check("editing an activity that does not exist is a 404", status == 404, f"status={status}")

    status, _ = jget(f"{LAN}/api/intake", method="POST",
                     data=json.dumps({"at": "whenever", "substance": "caffeine",
                                      "amount": 1}).encode(),
                     headers={"Content-Type": "application/json"})
    check("a malformed intake time is refused", status in (400, 422), f"status={status}")

    huge = "x" * 200_000
    status, _ = jget(f"{LAN}/api/journal/{TODAY}", method="PUT",
                     data=json.dumps({"tags": [f"t{i}" for i in range(500)],
                                      "amounts": {}, "notes": huge}).encode(),
                     headers={"Content-Type": "application/json"})
    check("an enormous journal entry is handled", status in (200, 413, 422), f"status={status}")
    check("server still healthy after an enormous journal entry", alive())

    status, body = jget(f"{LAN}/api/summary?days=9999")
    check("an out-of-range query parameter is refused, not obeyed",
          status == 422, f"status={status}")
finally:
    srv.stop()

sys.exit(report())
