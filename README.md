# whoop-bridge

Use a WHOOP 4.0 strap without the subscription. A **Windows laptop** collects
from the strap over Bluetooth LE and forwards to **your own server**, which
stores the data, computes the metrics, and serves one dashboard you can open
from the laptop or from your phone through a Cloudflare tunnel.

```
WHOOP 4.0  --BLE-->  Windows laptop        server (Docker)          your phone
                     tray app + bridge  ->  FastAPI + SQLite  <--   browser
                     (collector only)       + dashboard              via tunnel
```

The laptop is a collector, not a second UI — one dashboard, viewed from
everywhere. Because the laptop is around most of the time it acts as a standing
bridge: it reconnects on its own, pulls whatever the strap recorded while you
were away, and keeps a durable local queue so nothing is lost when the network
or the server is down.

Setup is in **[DEPLOY.md](DEPLOY.md)**.

> Independent, unofficial, not affiliated with WHOOP, Inc.
> See [`ATTRIBUTION.md`](ATTRIBUTION.md) for credits, licensing, and disclaimers.

## Why not NOOP?

NOOP is the well-known app for this, and it's good — but it ships for
**macOS and Android only**, and it's a native app rather than a bridge. There is
no Windows build. This project is a Windows-friendly, headless reimplementation
built from the same publicly documented protocol facts (credited in
[`ATTRIBUTION.md`](ATTRIBUTION.md)), not a port of its code.

## What you actually get

From the strap's ~14-day store (`HISTORICAL_DATA` v24), per record:

| Directly usable | Raw ADC counts only |
|---|---|
| heart rate (bpm) | SpO₂ red / IR |
| RR intervals → HRV | skin temperature |
| gravity/accel X, Y, Z (g), two triplets | respiratory rate |
| skin contact (on/off wrist) | ambient light, LED drive, PPG green & red/IR |
| device timestamp, signal quality | |

Plus live heart rate while connected, strap events (wrist on/off, charging,
battery, boot), and optionally raw IMU (accel + gyro, 100 Hz × 6 axes) and the
437 Hz PPG waveform.

**Be aware of the right-hand column.** SpO₂ %, skin temperature in °C, and
respiratory rate in breaths/min are computed in WHOOP's cloud from those
counts. The strap does not send real-world units, so this bridge forwards the
raw counts rather than inventing a calibration. If you want those in real
units you'd have to derive your own calibration.

## Learning features

The server also learns from your data over time — see
**[LEARNING.md](LEARNING.md)** for the detail and the limits.

- **Activity recognition.** Workouts, walks and sleep are detected from heart
  rate and movement, labelled by rules at first, then by a classifier trained
  on your own corrections. You can retype, retime, delete, or add activities by
  hand; your edits are never overwritten by re-detection.
- **Daily journal.** Tag days with alcohol, caffeine, stress, travel, illness
  or your own tags, plus notes.
- **What helps and what hurts.** Compares journalled factors against the *next*
  day's recovery, HRV, resting HR and sleep, using permutation tests with
  bootstrap confidence intervals and Benjamini-Hochberg FDR correction. It
  reports "no clear signal" honestly rather than inventing findings.
- **Suggestions.** Push today / take it easy / sleep debt, plus an illness
  signal from concordant deviations in resting HR, HRV, skin temperature and
  respiration.

The dashboard is dark by default with a light toggle, leads with a recovery
ring plus strain and sleep meters, and uses a bottom tab bar on phones. Colour
carries meaning only alongside a label, effect sizes are grouped by outcome so
units never share an axis, and charts have a hover/touch readout.

Every one of these stays off until it has enough data to be trustworthy, and
the **Learning** tab shows what is missing and roughly how long it will take —
typically a few days for activity recognition, about two weeks for insights.

## Metrics the server computes

Recovery (0–100), Strain (0–21), sleep blocks and performance, HRV (RMSSD with
Malik artifact filtering), resting heart rate, and wear time — all computed
locally from raw sensor data, with a rolling personal baseline.

**These are approximations, not WHOOP's numbers.** WHOOP computes recovery,
strain and sleep in its own cloud with undisclosed models; nothing here
reproduces them. These use published sports-science methods (Banister TRIMP for
strain, actigraphy-style sleep detection) and are internally consistent and
useful for tracking trends, but they will not match the official app. Not
medical measurements.

("Body battery" is a Garmin metric, not a WHOOP one — Recovery is the closest
equivalent here.)

## Setup

The laptop half. For the server and tunnel, see **[DEPLOY.md](DEPLOY.md)**.

Requires Windows 10 (build 16299+) or 11, Bluetooth LE, and Python 3.11+.

```powershell
git clone <this repo>
cd Whoop
powershell -ExecutionPolicy Bypass -File windows\setup.ps1
```

Then:

```powershell
# 1. Find the strap. Take it out of the official app first (see below).
.\.venv\Scripts\whoop-bridge.exe scan

# 2. Put the address and your https endpoint into config.toml

# 3. Check the endpoint accepts the payload shape
.\.venv\Scripts\whoop-bridge.exe test-endpoint

# 4. Run it
.\.venv\Scripts\whoop-bridge.exe run
```

For a system-tray app with pairing, status and a link to the dashboard:

```powershell
.\.venv\Scripts\pip.exe install -e ".[tray]"
.\.venv\Scripts\pythonw.exe -m tray.whoop_tray
```

Or headless, restarting on wake and at logon:

```powershell
.\windows\install-task.ps1
Start-ScheduledTask -TaskName WhoopBridge
```

### Important: the strap bonds to one device

A WHOOP 4.0 will only talk to one host at a time. **Un-pair it from the WHOOP
phone app** (and remove it from that phone's Bluetooth settings) before
scanning, or the laptop won't see it. Re-pairing to the phone later will
likewise take it away from the laptop.

## Configuration

See [`config.example.toml`](config.example.toml). Every option is commented.
Secrets are better supplied via environment variables than written to the file:

```powershell
$env:WHOOP_FORWARD_URL   = "https://your-endpoint.example/ingest"
$env:WHOOP_FORWARD_TOKEN = "your-token"
```

`config.toml` and `*.db` are gitignored so your endpoint, token and biometric
data don't get committed.

## What gets sent

Batched `POST` of newline-free JSON:

```json
{"records": [
  {"received_at": "2026-08-29T00:00:00+00:00",
   "packet": "HISTORICAL_DATA", "version": 24,
   "device_time": "2026-08-28T23:59:00+00:00",
   "heart_rate": 62, "rr_intervals_ms": [940, 955],
   "gravity_x": 0.01, "gravity_y": 0.02, "gravity_z": 0.99,
   "skin_contact": 1, "spo2_red": 12345, "skin_temp_raw": 7000,
   "resp_rate_raw": 321, "signal_quality": 88,
   "record_id": "…", "raw_hex": "aa…"}
]}
```

Headers: `Authorization: Bearer …` (if a token is set), `Idempotency-Key`
(SHA-256 of the body), and `X-Signature-SHA256` (HMAC, if `hmac_secret` is set)
so your receiver can verify the sender.

Delivery is **at-least-once**: records are deleted from the local spool only
after a 2xx, and each carries a stable `record_id` so your receiver can
de-duplicate. `https://` is required — the bridge refuses plain HTTP, because
this is biometric data in transit.

Every record also keeps `raw_hex`, so if a field mapping is later corrected you
can re-decode what you already collected instead of re-collecting it.

## A note on trimming

Acknowledging a chunk is what makes the strap send the next one — and it also
lets the strap delete that chunk. The bridge only ever acks **after** the
chunk's records are durably written to the local spool, so data is never
dropped from the band before it's stored. Setting `ack_and_trim = false` is
safer for the band's copy but stops the offload advancing past the first chunk.

## Troubleshooting

| Symptom | Cause |
|---|---|
| `scan` finds nothing | Strap still bonded to the phone app; or Bluetooth off; or strap flat |
| Connects, only live HR arrives | RTC not set — check `SET_CLOCK` in the log; historical data needs a valid clock |
| Offload stops after ~50 records | `ack_and_trim = false`, or acks are failing — check the log |
| Records queue but never send | Endpoint unreachable or returning non-2xx; run `whoop-bridge status` and `test-endpoint` |
| Commands seem ignored | Known on Windows/WinRT with unacknowledged writes; the bridge already uses confirmed writes throughout |

`whoop-bridge status` shows the queue depth at any time.

## Repository layout

| Path | What it is |
|---|---|
| `whoop_bridge/` | BLE collector: protocol, decode, spool, forwarder, CLI |
| `server/` | FastAPI app: ingest, analytics, dashboard, Dockerfile |
| `tray/` | Windows system-tray front end |
| `windows/` | Setup and Scheduled Task installers |
| `docker-compose.yml` | Server + cloudflared tunnel |

Server internals: `analytics.py` (metrics), `segment.py` (bout detection),
`ml.py` (activity classifier), `insights.py` (statistics), `advice.py`
(suggestions), `readiness.py` (progress and ETAs).

## Development

```bash
pip install -e . && pip install pytest
python -m pytest tests/ -q
```

Protocol details are in [`PROTOCOL.md`](PROTOCOL.md), deployment in
[`DEPLOY.md`](DEPLOY.md), and the learning features in
[`LEARNING.md`](LEARNING.md).
