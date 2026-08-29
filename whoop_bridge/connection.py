"""BLE transport: find the strap, run the sync handshake, keep it connected.

The handshake order matters. In particular SET_CLOCK is not optional: if the
strap's RTC is unset it silently refuses to serve historical (type-47) data,
which is where every metric other than live heart rate lives.

Historical offload is a chunked, acknowledged stream:

    SEND_HISTORICAL_DATA -> HISTORY_START
                         -> type-47 records ...
                         -> HISTORY_END   (we ack; strap may then trim)
                         -> more records / more ENDs ...
                         -> HISTORY_COMPLETE

Acking a HISTORY_END is what advances the offload, and it also permits the
strap to delete that chunk. So an ack is only ever sent after every record in
the chunk is durably in the spool -- data is never dropped from the band
before it is safely stored here.
"""

from __future__ import annotations

import asyncio
import logging
import time

from bleak import BleakClient, BleakScanner
from bleak.exc import BleakError

from . import protocol as P
from .decode import decode

log = logging.getLogger("whoop.ble")

META_HISTORY_START, META_HISTORY_END, META_HISTORY_COMPLETE = 1, 2, 3


async def scan(timeout: float = 12.0) -> list[tuple[str, str]]:
    """Return (address, name) for devices that look like a WHOOP strap."""
    found: dict[str, str] = {}
    wanted = {u.lower() for u in P.SERVICE_UUIDS}
    for _, (dev, adv) in (await BleakScanner.discover(timeout=timeout, return_adv=True)).items():
        name = (dev.name or adv.local_name or "").strip()
        if "whoop" in name.lower() or {u.lower() for u in (adv.service_uuids or [])} & wanted:
            found[dev.address] = name or "(unnamed)"
    return sorted(found.items())


class WhoopBridge:
    def __init__(self, address: str, spool, *, include_imu: bool = False,
                 live_hr: bool = True, backfill: bool = True,
                 ack_and_trim: bool = True, backfill_interval: float = 900.0,
                 reconnect_delay: float = 5.0, max_reconnect_delay: float = 120.0):
        self.address = address
        self.spool = spool
        self.include_imu = include_imu
        self.live_hr = live_hr
        self.backfill = backfill
        self.ack_and_trim = ack_and_trim
        self.backfill_interval = backfill_interval
        self.reconnect_delay = reconnect_delay
        self.max_reconnect_delay = max_reconnect_delay

        self._stop = asyncio.Event()
        self._seq = 0
        self._client: BleakClient | None = None
        self._pending = 0          # records spooled in the current chunk
        self._last_backfill = 0.0
        # Strong refs to in-flight ack tasks: asyncio only keeps weak ones, so
        # an unreferenced task can be garbage-collected before it runs.
        self._tasks: set[asyncio.Task] = set()
        # Live device state, published to the server so the dashboard can show
        # whether the strap is actually connected and how much charge it has.
        self.device = {
            "connected": False, "battery_pct": None, "battery_mv": None,
            "charging": None, "on_wrist": None, "last_packet_at": None,
            "last_connected_at": None, "address": address,
        }
        self.stats = {"frames": 0, "records": 0, "crc_errors": 0, "chunks_acked": 0}

    @property
    def is_connected(self) -> bool:
        return self._client is not None and self._client.is_connected

    def status(self, spool_depth: int) -> dict:
        """Snapshot for the server heartbeat."""
        return {**self.device, "connected": self.is_connected,
                "queued": spool_depth, "stats": dict(self.stats)}

    # --- command plumbing ---------------------------------------------------
    def _next_seq(self) -> int:
        self._seq = (self._seq + 1) & 0xFF
        return self._seq

    async def _send(self, cmd: int, payload: bytes = b"\x00") -> None:
        client = self._client
        if client is None or not client.is_connected:
            return
        frame = P.build_command(cmd, payload, seq=self._next_seq())
        # Confirmed writes throughout: unacknowledged writes on Windows/WinRT
        # are where "the strap randomly ignores commands" reports come from.
        await client.write_gatt_char(P.CHAR_CMD_TO_STRAP, frame, response=True)
        log.debug("-> cmd %d payload=%s", cmd, payload.hex())

    # --- notifications ------------------------------------------------------
    def _handler(self, source: str):
        def on_notify(_sender, data: bytearray) -> None:
            self._ingest(bytes(data), source)
        return on_notify

    def _on_battery(self, _sender, data: bytearray) -> None:
        """Standard 0x2A19: a single byte of percent."""
        if data:
            self.device["battery_pct"] = float(data[0])
            log.debug("battery %d%%", data[0])

    def _ingest(self, data: bytes, source: str) -> None:
        self.stats["frames"] += 1
        self.device["last_packet_at"] = _now()
        frame = P.parse_frame(data)
        if frame is None:
            self.spool.put({"source": source, "kind": "unparsed", "raw_hex": data.hex()})
            return
        if not frame.crc_ok:
            # Keep it -- a CRC failure is worth seeing downstream, not silently dropping.
            self.stats["crc_errors"] += 1

        record = decode(frame, source, include_imu=self.include_imu)
        self.spool.put(record)
        self.stats["records"] += 1

        # Events carry charge and wrist state; keep the live snapshot current.
        for key, field in (("battery_pct", "battery_pct"), ("battery_mv", "battery_mv"),
                           ("battery_charging", "charging"), ("on_wrist", "on_wrist")):
            if key in record:
                self.device[field] = record[key]

        if frame.packet_type == P.PacketType.HISTORICAL_DATA:
            self._pending += 1
        elif frame.packet_type == P.PacketType.METADATA:
            self._on_metadata(frame)

    def _on_metadata(self, frame) -> None:
        meta_type = frame.payload[0] if frame.payload else None
        if meta_type == META_HISTORY_START:
            log.info("historical offload started")
            self._pending = 0
        elif meta_type == META_HISTORY_END:
            # end_data lives at frame[17:25]; the ack must echo it verbatim.
            raw = frame.raw
            if len(raw) >= 25 and self.ack_and_trim:
                end_data = raw[17:25]
                # The record(s) in this chunk are already committed to the spool
                # above, so acking here cannot lose data.
                task = asyncio.create_task(self._ack_chunk(end_data))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
            elif not self.ack_and_trim:
                log.warning("HISTORY_END not acked (ack_and_trim disabled) -- "
                            "offload will not advance past this chunk")
        elif meta_type == META_HISTORY_COMPLETE:
            log.info("historical offload complete (%d records this session)",
                     self.stats["records"])

    async def _ack_chunk(self, end_data: bytes) -> None:
        try:
            await self._send(P.Cmd.HISTORICAL_DATA_RESULT, b"\x01" + end_data)
            self.stats["chunks_acked"] += 1
            log.info("acked chunk (%d records spooled, %d queued)",
                     self._pending, self.spool.depth())
            self._pending = 0
        except BleakError as exc:
            log.warning("chunk ack failed: %s", exc)

    # --- session ------------------------------------------------------------
    async def _handshake(self) -> None:
        await self._send(P.Cmd.GET_HELLO)
        await asyncio.sleep(0.2)
        await self._send(P.Cmd.REPORT_VERSION_INFO)
        await asyncio.sleep(0.2)
        # Mandatory: without a valid RTC the strap will not serve type-47 data.
        await self._send(P.Cmd.SET_CLOCK, P.set_clock_payload(time.time()))
        await asyncio.sleep(0.3)
        await self._send(P.Cmd.GET_BATTERY_LEVEL)
        await asyncio.sleep(0.2)
        await self._send(P.Cmd.GET_DATA_RANGE)
        await asyncio.sleep(0.2)

        if self.live_hr:
            await self._send(P.Cmd.TOGGLE_REALTIME_HR, b"\x01")
            await asyncio.sleep(0.2)
        if self.include_imu:
            # High-volume streams; only enabled when explicitly configured.
            await self._send(P.Cmd.TOGGLE_IMU_MODE, b"\x01")
            await asyncio.sleep(0.2)
            await self._send(P.Cmd.ENABLE_OPTICAL_DATA, b"\x01")
            await asyncio.sleep(0.2)
        log.info("handshake complete")

    async def _start_backfill(self) -> None:
        await self._send(P.Cmd.ENTER_HIGH_FREQ_SYNC)
        await asyncio.sleep(0.3)
        # Payload must be [0x00]; an empty payload yields zero frames.
        await self._send(P.Cmd.SEND_HISTORICAL_DATA, b"\x00")
        self._last_backfill = time.monotonic()
        log.info("requested historical offload")

    async def _session(self) -> None:
        log.info("connecting to %s", self.address)
        async with BleakClient(self.address, timeout=30.0) as client:
            self._client = client
            log.info("connected")
            self.device["last_connected_at"] = _now()
            available = {c.uuid.lower() for s in client.services for c in s.characteristics}

            # Standard battery service, if the strap exposes it.
            if P.CHAR_BATTERY_LEVEL.lower() in available:
                try:
                    value = await client.read_gatt_char(P.CHAR_BATTERY_LEVEL)
                    self._on_battery(None, value)
                    await client.start_notify(P.CHAR_BATTERY_LEVEL, self._on_battery)
                except BleakError as exc:
                    log.debug("battery characteristic unavailable: %s", exc)

            for uuid in P.NOTIFY_CHARS:
                if uuid.lower() in available:
                    await client.start_notify(uuid, self._handler(_label(uuid)))
                else:
                    log.warning("characteristic %s missing on this strap", _label(uuid))

            await self._handshake()
            if self.backfill:
                await self._start_backfill()

            while not self._stop.is_set() and client.is_connected:
                await asyncio.sleep(1.0)
                # Re-request the offload periodically so records recorded while
                # connected still make it across.
                if (self.backfill
                        and time.monotonic() - self._last_backfill > self.backfill_interval):
                    await self._start_backfill()

            if client.is_connected:
                await self._send(P.Cmd.EXIT_HIGH_FREQ_SYNC)
        self._client = None
        self.device["connected"] = False

    async def run(self) -> None:
        delay = self.reconnect_delay
        while not self._stop.is_set():
            try:
                await self._session()
                delay = self.reconnect_delay
            except BleakError as exc:
                log.warning("BLE error: %s", exc)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a long-running bridge must not die
                log.exception("unexpected error in BLE session")
            if self._stop.is_set():
                break
            log.info("reconnecting in %.0fs (spool depth %d)", delay, self.spool.depth())
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass
            delay = min(delay * 2, self.max_reconnect_delay)

    def stop(self) -> None:
        self._stop.set()


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _label(uuid: str) -> str:
    return {
        P.CHAR_CMD_FROM_STRAP: "cmd",
        P.CHAR_EVENTS_FROM_STRAP: "events",
        P.CHAR_DATA_FROM_STRAP: "data",
    }.get(uuid.lower(), uuid)
