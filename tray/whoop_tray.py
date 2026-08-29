"""Windows system-tray front end for the bridge.

Deliberately thin: pair, connect, and see whether data is flowing. All stats
live in the server dashboard, which the laptop and the phone both open through
the Cloudflare tunnel -- one UI, not two.

Run with:  pythonw -m tray.whoop_tray
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path

try:
    import pystray
    from PIL import Image, ImageDraw
except ImportError:
    sys.exit("Tray needs extra packages. Install with: pip install -e .[tray]")

from whoop_bridge.config import Config
from whoop_bridge.connection import WhoopBridge, scan
from whoop_bridge.forwarder import Forwarder
from whoop_bridge.heartbeat import Heartbeat
from whoop_bridge.spool import Spool

log = logging.getLogger("whoop.tray")

GREY, GREEN, AMBER, RED = "#8b949e", "#2ea861", "#d29922", "#e5534b"


def make_icon(colour: str) -> "Image.Image":
    """A filled dot in the given state colour."""
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    ImageDraw.Draw(img).ellipse((10, 10, 54, 54), fill=colour)
    return img


class TrayApp:
    def __init__(self, config_path: str = "config.toml"):
        self.config_path = Path(config_path)
        self.cfg = Config.load(self.config_path) if self.config_path.exists() else Config()
        self.spool = Spool(self.cfg.spool_path)
        self.bridge: WhoopBridge | None = None
        self.forwarder: Forwarder | None = None
        self.heartbeat: Heartbeat | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.thread: threading.Thread | None = None
        self.status = "stopped"
        self.icon = pystray.Icon("whoop", make_icon(GREY), "Whoop bridge", self._menu())

    # --- menu ---------------------------------------------------------------
    def _menu(self) -> "pystray.Menu":
        return pystray.Menu(
            pystray.MenuItem(lambda _: self._title(), None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Start", self.start, enabled=lambda _: not self._running()),
            pystray.MenuItem("Stop", self.stop, enabled=lambda _: self._running()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Find my strap…", self.do_scan),
            pystray.MenuItem("Open dashboard", self.open_dashboard),
            pystray.MenuItem("Edit config", self.edit_config),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", self.quit),
        )

    def _title(self) -> str:
        queued = self.spool.depth()
        stats = self.bridge.stats if self.bridge else {}
        battery = (self.bridge.device.get("battery_pct") if self.bridge else None)
        line = f"{self.status}  ·  {queued} queued"
        if battery is not None:
            line += f"  ·  {battery:.0f}% battery"
        if stats.get("records"):
            line += f"  ·  {stats['records']} records"
        return line

    def _running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def _set(self, status: str, colour: str) -> None:
        self.status = status
        self.icon.icon = make_icon(colour)
        self.icon.title = f"Whoop bridge — {self._title()}"

    # --- actions ------------------------------------------------------------
    def start(self, *_) -> None:
        if self._running():
            return
        problems = self.cfg.validate()
        if problems:
            self.icon.notify("\n".join(problems), "Configuration incomplete")
            self._set("misconfigured", RED)
            return

        self.bridge = WhoopBridge(
            self.cfg.address, self.spool,
            include_imu=self.cfg.include_imu, live_hr=self.cfg.live_hr,
            backfill=self.cfg.backfill, ack_and_trim=self.cfg.ack_and_trim,
            backfill_interval=self.cfg.backfill_interval,
        )
        self.forwarder = Forwarder(
            self.spool, url=self.cfg.forward_url,
            token=self.cfg.forward_token or None,
            hmac_secret=self.cfg.hmac_secret or None,
            batch_size=self.cfg.batch_size, interval=self.cfg.forward_interval,
            verify_tls=self.cfg.verify_tls,
            cf_access_client_id=self.cfg.cf_access_client_id or None,
            cf_access_client_secret=self.cfg.cf_access_client_secret or None,
        )
        self.heartbeat = Heartbeat(
            self.bridge, self.spool, url=self.cfg.forward_url,
            token=self.cfg.forward_token or None, interval=self.cfg.heartbeat_interval,
            verify_tls=self.cfg.verify_tls,
            cf_access_client_id=self.cfg.cf_access_client_id or None,
            cf_access_client_secret=self.cfg.cf_access_client_secret or None,
        )
        self.thread = threading.Thread(target=self._run_loop, daemon=True)
        self.thread.start()
        self._set("connecting", AMBER)

    def _run_loop(self) -> None:
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(asyncio.gather(
                self.bridge.run(), self.forwarder.run(), self.heartbeat.run()))
        except Exception:
            log.exception("bridge stopped unexpectedly")
            self._set("error", RED)
        finally:
            self.loop.close()

    def stop(self, *_) -> None:
        if self.bridge:
            self.bridge.stop()
        if self.forwarder:
            self.forwarder.stop()
        if self.heartbeat:
            self.heartbeat.stop()
        self._set("stopped", GREY)

    def do_scan(self, *_) -> None:
        def worker() -> None:
            self.icon.notify("Scanning for 12 seconds…", "Whoop bridge")
            try:
                found = asyncio.run(scan(12.0))
            except Exception as exc:
                self.icon.notify(str(exc), "Scan failed")
                return
            if not found:
                self.icon.notify(
                    "No strap found. Un-pair it from the WHOOP phone app first — "
                    "the strap only talks to one device at a time.", "Nothing found")
                return
            self.icon.notify("\n".join(f"{a}  {n}" for a, n in found)
                             + "\n\nPut the address in config.toml.", "Straps found")
        threading.Thread(target=worker, daemon=True).start()

    def open_dashboard(self, *_) -> None:
        url = self.cfg.forward_url.rsplit("/ingest", 1)[0] if self.cfg.forward_url else ""
        if url:
            webbrowser.open(url)
        else:
            self.icon.notify("No forward_url configured yet.", "Whoop bridge")

    def edit_config(self, *_) -> None:
        if self.config_path.exists():
            subprocess.Popen(["notepad.exe", str(self.config_path)])
        else:
            self.icon.notify(f"{self.config_path} not found.", "Whoop bridge")

    def quit(self, *_) -> None:
        self.stop()
        self.icon.stop()

    def run(self) -> None:
        # Poll the bridge so the icon colour reflects what is actually happening.
        def poll() -> None:
            import time
            while True:
                time.sleep(5)
                if self._running() and self.bridge:
                    connected = self.bridge.is_connected
                    queued = self.spool.depth()
                    if not connected:
                        self._set("searching for strap", AMBER)
                    elif queued > 5000:
                        self._set("connected, endpoint backed up", AMBER)
                    else:
                        self._set("connected", GREEN)
        threading.Thread(target=poll, daemon=True).start()
        self.icon.run()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    TrayApp(sys.argv[1] if len(sys.argv) > 1 else "config.toml").run()


if __name__ == "__main__":
    main()
