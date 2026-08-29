"""The Windows laptop, minus the Bluetooth radio.

Everything below the radio is the real thing: frames go through
protocol.parse_frame, decode(), the real Spool and the real Forwarder. Only
`bleak` is replaced, because there is no strap in this container.

Run as a subprocess so it behaves like the always-on process it really is:
it can be killed, restarted, and left running across a server outage.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import strap                                              # noqa: E402
from whoop_bridge.config import Config                    # noqa: E402
from whoop_bridge.decode import decode                    # noqa: E402
from whoop_bridge.forwarder import Forwarder              # noqa: E402
from whoop_bridge.protocol import parse_frame             # noqa: E402
from whoop_bridge.spool import Spool                      # noqa: E402

log = logging.getLogger("laptop")


def ingest_frame(spool: Spool, raw: bytes, source: str) -> dict | None:
    """Exactly what connection.WhoopBridge._ingest does with a notification."""
    frame = parse_frame(raw)
    if frame is None:
        spool.put({"source": source, "kind": "unparsed", "raw_hex": raw.hex()})
        return None
    record = decode(frame, source)
    spool.put(record)
    return record


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--spool", required=True)
    ap.add_argument("--url", default="")           # override the config's URL
    ap.add_argument("--backfill-minutes", type=int, default=0)
    ap.add_argument("--backfill-start", type=int, default=0)
    ap.add_argument("--live-seconds", type=float, default=0.0)
    ap.add_argument("--live-rate", type=float, default=5.0)   # records/second
    ap.add_argument("--seed", type=int, default=3)
    ap.add_argument("--stats", default="")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    cfg = Config.load(args.config)
    url = args.url or cfg.forward_url
    spool = Spool(args.spool)

    produced = {"records": 0, "ids": []}

    if args.backfill_minutes:
        start = args.backfill_start or int(time.time()) - args.backfill_minutes * 60
        for raw in strap.day_of_frames(start, args.backfill_minutes, seed=args.seed,
                                       sleep_from=0, sleep_to=min(420, args.backfill_minutes),
                                       workout=(1020, 1075) if args.backfill_minutes > 1080 else None):
            rec = ingest_frame(spool, raw, "data")
            if rec:
                produced["records"] += 1
                produced["ids"].append(rec["record_id"])
        log.info("backfilled %d records into the spool", produced["records"])

    forwarder = Forwarder(
        spool, url=url, token=cfg.forward_token or None,
        hmac_secret=cfg.hmac_secret or None, batch_size=cfg.batch_size,
        interval=1.0, verify_tls=cfg.verify_tls,
        cf_access_client_id=cfg.cf_access_client_id or None,
        cf_access_client_secret=cfg.cf_access_client_secret or None,
    )
    task = asyncio.create_task(forwarder.run())

    if args.live_seconds:
        rng = random.Random(args.seed + 100)
        deadline = time.monotonic() + args.live_seconds
        unix = int(time.time())
        n = 0
        while time.monotonic() < deadline:
            hr = int(74 + 14 * random.random() + rng.gauss(0, 3))
            mean = 60000 / hr
            rr = [int(max(300, rng.gauss(mean, 24))) for _ in range(4)]
            rec = ingest_frame(spool, strap.historical(unix + n, hr, rr, rng=rng), "data")
            if rec:
                produced["records"] += 1
                produced["ids"].append(rec["record_id"])
            n += 1
            if n % 60 == 0:
                ingest_frame(spool, strap.battery_event(80 - n / 600), "events")
            await asyncio.sleep(1.0 / args.live_rate)

    # Give the forwarder a chance to drain what is left.
    for _ in range(120):
        if spool.depth() == 0:
            break
        await asyncio.sleep(0.5)

    forwarder.stop()
    await asyncio.wait_for(task, timeout=20)
    if args.stats:
        json.dump({"produced": produced["records"], "ids": produced["ids"],
                   "spool_depth": spool.depth()}, open(args.stats, "w"))
    log.info("done: produced=%d spool_depth=%d", produced["records"], spool.depth())


if __name__ == "__main__":
    asyncio.run(main())
