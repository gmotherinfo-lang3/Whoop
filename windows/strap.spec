# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec: one Strap.exe, no Python install needed.

The Python-from-python.org step is the one most likely to defeat someone
setting this up, and it is the only step that cannot be explained away. This
removes it: one file, double-click, done.

Built on Windows -- PyInstaller does not cross-compile, so this runs in CI on
a Windows runner (see .github/workflows/windows-app.yml), not on the server.
"""

import sys
from pathlib import Path

ROOT = Path(SPECPATH).parent

# bleak reaches for the Windows Runtime at import time and PyInstaller cannot
# see those imports, so they are named here. Missing one shows up as a working
# app that cannot find any strap, which is a miserable thing to debug.
HIDDEN = [
    "bleak.backends.winrt",
    "bleak.backends.winrt.client",
    "bleak.backends.winrt.scanner",
    "bleak.backends.winrt.util",
    "winrt.windows.devices.bluetooth",
    "winrt.windows.devices.bluetooth.advertisement",
    "winrt.windows.devices.bluetooth.genericattributeprofile",
    "winrt.windows.devices.enumeration",
    "winrt.windows.foundation",
    "winrt.windows.foundation.collections",
    "winrt.windows.storage.streams",
    "pystray._win32",
    "PIL._tkinter_finder",
    "tkinter",
    "tkinter.ttk",
]

analysis = Analysis(
    [str(ROOT / "windows" / "launcher.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[(str(ROOT / "config.example.toml"), ".")],
    hiddenimports=HIDDEN,
    hookspath=[],
    runtime_hooks=[],
    # Trimming what a tray app cannot need keeps the download reasonable.
    excludes=["matplotlib", "numpy", "scipy", "pandas", "pytest", "IPython"],
    noarchive=False,
    collect_all=["winrt"],
)
pyz = PYZ(analysis.pure)

exe = EXE(
    pyz,
    analysis.scripts,
    analysis.binaries,
    analysis.datas,
    [],
    name="Strap",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    # No console window: this is a tray app, and a black box appearing at
    # logon looks like something has gone wrong.
    console=False,
    disable_windowed_traceback=False,
    icon=None,
)
