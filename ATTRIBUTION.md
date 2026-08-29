# Attribution and licensing

## What this project is

An independent Python bridge that lets someone who **owns** a WHOOP 4.0 strap
read **their own** biometric data from **their own** device on a machine they
control. It is not affiliated with, endorsed by, or connected to WHOOP, Inc.
"WHOOP" is used nominatively, only to identify the hardware this interoperates
with.

## Where the protocol knowledge comes from

The protocol facts in `PROTOCOL.md` — frame layout, CRC parameters, command
numbers, field offsets — were compiled from public community
reverse-engineering work:

- **NOOP / Strand** (`muftiarfan/noop`) — the schema-driven WHOOP 4.0 protocol
  description, the historical v24 field map, the high-frequency-sync offload
  sequence, and the `SET_CLOCK`/RTC requirement.
- **`johnmiddleton12/my-whoop`** — the upstream `WhoopProtocol` framing and
  decode work that NOOP vendors.
- **`bWanShiTong/reverse-engineering-whoop-post`** — the public write-up of
  frame framing, command categories, and the sync flow.
- **`christianmeurer/whoop-reader`** — GATT characteristic layout and the
  Python/Bleak approach.

## What was and was not copied

**No third-party source code is copied into this repository.** Every file here
is an original Python implementation written against the *facts* listed in
`PROTOCOL.md`.

This distinction is deliberate and it matters:

- Protocol facts — which byte is at which offset, what a CRC polynomial is,
  what number a command has — are **uncopyrightable factual information** about
  how bytes appear on a wire.
- The **expression** of those facts in someone else's source code is
  copyrighted, and NOOP in particular ships **no LICENSE file**, which means
  default "all rights reserved" applies to its code. Copying its Swift into
  this repo would have carried that restriction here.

Re-implementing from documented facts avoids that entirely. The credit above is
owed and gladly given.

This repository contains no WHOOP application binaries, firmware, decompiled
code, logos, or assets, and no account credentials or API secrets. It
circumvents no technological protection measure and bypasses no subscription,
paywall, or login — it reads a device the user already owns.

## Warranty and medical disclaimer

Provided as-is, for personal and educational use, with no warranty of any kind.
Use at your own risk, including risk to your device and its warranty status.

Heart rate, HRV, SpO₂, respiration, and temperature values are **approximations
from an unofficial decoder**, are not clinically validated, are not a medical
device, and are not medical advice. Several values (SpO₂, skin temperature,
respiratory rate) are forwarded as **raw ADC counts**, not real-world units.
Do not use any of this to make health decisions.
