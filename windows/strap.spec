# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec: one Strap.exe, no Python install needed.

The Python-from-python.org step is the one most likely to defeat someone
setting this up, and it is the only step that cannot be explained away. This
removes it: one file, double-click, done.

Built on Windows -- PyInstaller does not cross-compile, so this runs in CI on
a Windows runner (see .github/workflows/windows-app.yml), not on the server.

  About winrt

bleak talks to Windows Bluetooth through the `winrt-*` packages, which are
namespace packages split across a dozen distributions. PyInstaller's static
analysis cannot see into them, and a hand-written list of module names is a
guess that goes stale whenever bleak changes what it needs. So the list is
discovered here, on the machine doing the build, from what is actually
installed. If nothing is found the build fails rather than producing an exe
that starts cleanly and then never finds a strap.
"""

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_dynamic_libs, collect_submodules

ROOT = Path(SPECPATH).parent

# Discovered rather than declared: whatever winrt packages bleak pulled in.
winrt_modules = collect_submodules("winrt")
winrt_binaries = collect_dynamic_libs("winrt")

if sys.platform == "win32" and not winrt_modules:
    raise SystemExit(
        "No winrt modules found. bleak cannot reach Windows Bluetooth without "
        "them, so this build would produce an app that never finds a strap.\n"
        "Install the Windows dependencies first:  pip install -e \".[tray]\"")

HIDDEN = [
    *winrt_modules,
    "bleak.backends.winrt",
    "bleak.backends.winrt.client",
    "bleak.backends.winrt.scanner",
    "bleak.backends.winrt.util",
    "pystray._win32",
    "PIL._tkinter_finder",
    "tkinter",
    "tkinter.ttk",
]

analysis = Analysis(
    [str(ROOT / "windows" / "launcher.py")],
    pathex=[str(ROOT)],
    binaries=winrt_binaries,
    datas=[(str(ROOT / "config.example.toml"), ".")],
    hiddenimports=HIDDEN,
    hookspath=[],
    runtime_hooks=[],
    # Trimming what a tray app cannot need keeps the download reasonable.
    # winrt is deliberately NOT here: excluding it is how this build ends up
    # unable to see any Bluetooth adapter.
    excludes=["matplotlib", "numpy", "scipy", "pandas", "pytest", "IPython"],
    noarchive=False,
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
