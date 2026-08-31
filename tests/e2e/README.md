# End-to-end tests

These drive the whole system the way it is actually used: a server, a Windows
laptop bridging a strap over Bluetooth, and a phone reaching the same server
through a Cloudflare tunnel.

Almost nothing is mocked. The phases run the real FastAPI server as a
subprocess, the real `Spool` and `Forwarder` from `whoop_bridge`, real
`parse_frame`/`decode` over CRC-valid frames, and real Chromium for the two
browser clients. Two things stand in for hardware that is not present:

| File | Stands in for | How faithfully |
|---|---|---|
| `strap.py` | the WHOOP 4.0 radio | builds real frames — SOF, length, CRC-8, CRC-32, v24 field offsets — so `parse_frame` and `decode` are genuinely exercised |
| `tunnel.py` | `cloudflared` + Cloudflare Access | adds `CF-Connecting-IP`; adds the authenticated-user-email header only for a browser session, never for a service token, which is what decides who may download the setup bundle; bounces everything else with a 302 at the edge |

## Running

```bash
pip install -e . && pip install pytest playwright starlette httpx
python tests/e2e/run.py              # every phase
python tests/e2e/run.py nasty input  # a couple of them
```

`E2E_CHROME` overrides the browser path; `E2E_WORK` overrides where databases,
spools and logs are written. The phases use fixed ports (8401-8472) on 127.0.0.1, so run one at a time
rather than two in parallel.

These are deliberately **not** collected by `pytest`: they start servers, take
minutes, and need a browser. `python -m pytest tests/` stays fast.

## The phases

| Phase | What it establishes |
|---|---|
| `setup` | a brand-new install with an empty database answers every endpoint; the bundle is served to a LAN client and to a logged-in browser, and refused to a service token and to an unauthenticated request; the downloaded config needs nothing but the strap's address |
| `bridge` | the bridge delivers through the tunnel with a service token; a mid-stream server outage loses nothing and duplicates nothing; a replayed batch is absorbed; the update channel works and still needs the ingest token |
| `nasty` | a strap with a lost clock, a clock running ahead, out-of-order bursts, missing fields, wrong types, absurd values, 5000-record catch-ups, malformed bodies and bad writes from the app |
| `poison` | one record the database cannot store does not stall the queue — see below |
| `input` | a half-written journal entry survives leaving the app and coming back, and refreshes resume once it is saved |
| `clients` | the laptop and the phone at once: the phone writes journal entries, activities and intake through the tunnel while the strap streams, and the laptop sees it |
| `resilience` | the laptop reboots mid-queue; the phone sees the strap's battery and connection state; the tunnel falls over and the LAN carries on; both clients save at the same moment; the phone writes under a heavy ingest load |
| `polish` | the ring bar clears the phone's status bar; entrance animations run on a navigation and never on a background refresh; a value that moved during a refresh is highlighted; and the CSV export downloads, in every grain, with the right shape and the right guard rails |

## Why `poison` exists

The bridge deletes a spooled row only after a 2xx and retries every 5xx
forever. So a record the server cannot store is not one lost record — it is a
queue that never drains again, and a user whose data silently stops arriving
while the app still reports the bridge as connected. `poison` puts one
unstorable record in the middle of eighty good ones and requires the queue to
drain anyway. The unit-test counterpart lives in `tests/test_live.py`.
