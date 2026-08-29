# WHOOP 4.0 BLE protocol reference

Factual notes on how the strap's bytes appear on the wire, as implemented in
`whoop_bridge/protocol.py` and `whoop_bridge/decode.py`. WHOOP publishes no
specification; see `ATTRIBUTION.md` for where these facts come from.

## GATT

Vendor service `61080000-8d6d-82b8-614a-1c8cb0f8dcc6` (some sources report the
service as `...0002-...`; the bridge probes both).

| Characteristic | UUID suffix | Direction |
|---|---|---|
| `CMD_TO_STRAP` | `61080010-…` | write |
| `CMD_FROM_STRAP` | `61080012-…` | notify |
| `EVENTS_FROM_STRAP` | `61080015-…` | notify |
| `DATA_FROM_STRAP` | `61080018-…` | notify |
| `MEMFAULT` | `6108001b-…` | notify (diagnostics) |

## Frame envelope

```
off 0    1 byte    0xAA  start of frame
off 1    2 bytes   length, u16 LE
off 3    1 byte    CRC-8 over the two length bytes
off 4    1 byte    packet type
off 5    1 byte    sequence (for HISTORICAL_DATA this is the record VERSION)
off 6..  n bytes   packet body (command number first, for COMMAND packets)
off len  4 bytes   CRC-32, u32 LE
```

`length` counts from offset 4 through the end of the CRC-32, so the total frame
size is `length + 4`, and the CRC-32 covers `frame[4:length]`.

- **CRC-8**: poly `0x07`, init `0x00`, no reflection, no final XOR.
  Verified: `aa 10 00 -> 0x57`, `aa 08 00 -> 0xa8`.
- **CRC-32**: **standard zlib** (poly `0xEDB88320` reflected, init and final XOR
  `0xFFFFFFFF`). Several public write-ups claim a custom final XOR of
  `0xF43F44AC` — that is not what this strap uses.

## Packet types

| # | Name |
|---|---|
| 35 | COMMAND |
| 36 | COMMAND_RESPONSE |
| 40 | REALTIME_DATA |
| 43 | REALTIME_RAW_DATA |
| 47 | HISTORICAL_DATA |
| 48 | EVENT |
| 49 | METADATA |
| 50 | CONSOLE_LOGS |

## Commands used by this bridge

| # | Name | Note |
|---|---|---|
| 3 | `TOGGLE_REALTIME_HR` | `01` on, `00` off |
| 7 | `REPORT_VERSION_INFO` | |
| 10 | `SET_CLOCK` | payload `[secs u32 LE][subsecs u32 LE]`, **exactly 8 bytes** |
| 22 | `SEND_HISTORICAL_DATA` | payload must be `00`, not empty |
| 23 | `HISTORICAL_DATA_RESULT` | chunk ack, payload `01` + `end_data` |
| 26 | `GET_BATTERY_LEVEL` | |
| 34 | `GET_DATA_RANGE` | |
| 96 / 97 | `ENTER` / `EXIT_HIGH_FREQ_SYNC` | |
| 106 / 107 | `TOGGLE_IMU_MODE` / `ENABLE_OPTICAL_DATA` | high-volume streams |
| 145 | `GET_HELLO` | |

Destructive commands (`FORCE_TRIM` 25, `REBOOT_STRAP` 29, `POWER_CYCLE` 32,
firmware load 36–38 / 142–144) are deliberately not exposed by this bridge.

**`SET_CLOCK` is not optional.** If the strap's RTC is unset it accepts the
command but silently refuses to serve HISTORICAL_DATA — which is where every
metric other than live heart rate lives. A wrong-length payload is acked but
not latched, with the same result.

## HISTORICAL_DATA (type 47), version 24

The ~14-day biometric store, and the main reason to run this bridge. The
version is in the frame's `seq` byte; v12 shares v24's layout. Offsets are
frame-absolute.

| Offset | Type | Field | Note |
|---|---|---|---|
| 11 | u32 | `unix` | real unix seconds |
| 21 | u8 | `heart_rate` | bpm |
| 22 | u8 | `rr_count` | RR intervals follow at offset 23, u16 each |
| 33 | u16 | `ppg_green` | green LED ADC |
| 35 | u16 | `ppg_red_ir` | red/IR LED ADC |
| 40/44/48 | f32 | `gravity_x/y/z` | g |
| 55 | u8 | `skin_contact` | 0 = off-wrist |
| 56/60/64 | f32 | `gravity2_x/y/z` | second accel triplet, g |
| 68 | u16 | `spo2_red` | raw ADC |
| 70 | u16 | `spo2_ir` | raw ADC |
| 72 | u16 | `skin_temp_raw` | raw ADC |
| 74 | u16 | `ambient_light` | raw ADC |
| 76/78 | u16 | `led_drive_1/2` | |
| 80 | u16 | `resp_rate_raw` | raw |
| 82 | u16 | `signal_quality` | |

**Raw ADCs are raw.** SpO₂ %, skin temperature in °C, and respiratory rate in
breaths/min are computed by WHOOP's cloud from these counts, not by the strap.
The bridge forwards the counts unconverted rather than inventing a calibration.
Heart rate, RR intervals (and therefore HRV), gravity/accelerometer vectors and
skin-contact are directly usable.

## Historical offload sequence

```
ENTER_HIGH_FREQ_SYNC
SEND_HISTORICAL_DATA (payload 00)
  -> METADATA HISTORY_START
  -> HISTORICAL_DATA records ...
  -> METADATA HISTORY_END      -> ack: HISTORICAL_DATA_RESULT, 01 + frame[17:25]
  -> more records / more ENDs ...
  -> METADATA HISTORY_COMPLETE
```

High-frequency sync sends **one** `HISTORY_START` then **repeated**
`HISTORY_END`s (a chunk close roughly every 50 records). Every END must be
acked — that is what advances the offload — and the ack must echo
`frame[17:25]` verbatim. Acking also permits the strap to trim (delete) that
chunk, so the bridge only acks after the chunk's records are durably in the
spool.

## REALTIME_RAW_DATA (type 43)

Selected by total frame length:

- **1917 — IMU**: 100 samples per axis at ~100 Hz, signed int16 LE. Accel at
  offsets 89/289/489, scale `1/4096` g per LSB. Gyro at 692/892/1092, scale
  `2000/32768` = 0.06104 deg/s per LSB.
- **1921 — PPG**: a single AC-coupled waveform (not 4 interleaved channels).
  Samples from offset 42, stride 4, signed 24-bit LE; byte 3 of each group is an
  unmapped aux byte. ~419 samples per packet at ~437 Hz.

Both are high-volume and off by default (`include_imu`).
