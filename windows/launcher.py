"""Entry point for the packaged Windows app.

A frozen build has no working directory to speak of and no `python -m`, so
this sorts out where the config and spool live before handing over to the
tray. Kept separate from tray/whoop_tray.py so the source install is
unaffected by any of it.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def app_dir() -> Path:
    """Where this laptop's config, spool and log belong.

    Next to the exe if that is writable -- people expect a portable app to
    keep its files together -- and otherwise under %LOCALAPPDATA%, which is
    what happens when it has been dropped in Program Files.
    """
    if getattr(sys, "frozen", False):
        beside = Path(sys.executable).parent
        probe = beside / ".writable"
        try:
            probe.touch()
            probe.unlink()
            return beside
        except OSError:
            pass
    else:
        return Path.cwd()
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    target = Path(base) / "Strap"
    target.mkdir(parents=True, exist_ok=True)
    return target


def self_check() -> int:
    """Verify the frozen bundle actually contains what it needs, and exit.

    PyInstaller silently omits imports it cannot see -- bleak's Windows
    Runtime backend and tkinter are both easy to lose, and both fail later as
    "no strap found" or a setup window that never opens, which are miserable
    things to diagnose from a user's description. CI runs this against the
    built exe so a broken bundle never ships.
    """
    required = [
        ("tkinter", "the first-run setup window"),
        ("bleak", "talking to the strap"),
        ("bleak.backends.winrt.client", "Bluetooth on Windows"),
        ("pystray", "the tray icon"),
        ("PIL", "the tray icon"),
        ("httpx", "sending records to your server"),
        ("whoop_bridge.protocol", "decoding what the strap sends"),
        ("whoop_bridge.setup_config", "pairing"),
        ("tray.setup_window", "the first-run setup window"),
    ]
    missing, unhappy = [], []
    for module, why in required:
        if module == "bleak.backends.winrt.client" and sys.platform != "win32":
            continue
        try:
            __import__(module)
        except ImportError as exc:
            # Genuinely not in the bundle: this build is broken.
            missing.append(f"  {module} ({why}): {exc}")
        except Exception as exc:                          # noqa: BLE001
            # Present, but would not start here -- a tray icon needs a desktop,
            # for instance. Worth printing, not worth failing the build over.
            unhappy.append(f"  {module}: {exc}")
    for line in unhappy:
        print("present but did not initialise here:" if line is unhappy[0] else "", line)
    if missing:
        print("This build is missing:\n" + "\n".join(missing))
        return 1
    print(f"ok: {len(required)} required modules present, files in {app_dir()}")
    return 0


def main() -> None:
    if "--check" in sys.argv:
        raise SystemExit(self_check())
    home = app_dir()
    os.chdir(home)
    config = home / "config.toml"

    # A first run with nothing beside it still needs something to edit, so the
    # example is unpacked once and then left alone.
    if not config.exists():
        bundled = Path(getattr(sys, "_MEIPASS", Path(__file__).parent.parent)) / "config.example.toml"
        if bundled.exists():
            config.write_text(bundled.read_text(encoding="utf-8"), encoding="utf-8")

    sys.argv = [sys.argv[0], str(config), *sys.argv[1:]]
    from tray.whoop_tray import main as tray_main
    tray_main()


if __name__ == "__main__":
    main()
