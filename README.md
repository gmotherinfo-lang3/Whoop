# whoop-bridge

Use a WHOOP 4.0 strap without the subscription: pair it to a **Windows laptop**
over Bluetooth LE, pull the data off it, and forward that data to your own
endpoint.

Because the laptop is around most of the time, it acts as a standing bridge —
it reconnects on its own, pulls whatever the strap recorded while you were
away, and keeps a durable local queue so nothing is lost when the network or
the endpoint is down.

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

## Setup

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

To keep it running in the background, restarting on wake and at logon:

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

## Development

```bash
pip install -e . && pip install pytest
python -m pytest tests/ -q
```

Protocol details are documented in [`PROTOCOL.md`](PROTOCOL.md).
