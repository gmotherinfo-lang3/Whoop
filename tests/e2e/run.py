#!/usr/bin/env python3
"""Run the end-to-end phases.

    python tests/e2e/run.py            # everything
    python tests/e2e/run.py nasty      # one or more phases by name

Each phase runs the real server, the real bridge code and, where the phase
needs a browser, real Chromium. Nothing here is mocked except the Bluetooth
radio and Cloudflare itself, both of which are stood in for faithfully -- see
strap.py and tunnel.py.
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PHASES = ["setup", "bridge", "nasty", "poison", "input", "clients", "resilience"]


def main() -> int:
    wanted = sys.argv[1:] or PHASES
    unknown = [w for w in wanted if w not in PHASES]
    if unknown:
        print(f"unknown phase(s): {', '.join(unknown)}\nknown: {', '.join(PHASES)}")
        return 2

    failed = []
    for name in wanted:
        print(f"\n{'=' * 72}\n  {name}\n{'=' * 72}", flush=True)
        t0 = time.time()
        rc = subprocess.call([sys.executable, str(HERE / f"phase_{name}.py")], cwd=HERE)
        print(f"  [{name}] {'ok' if rc == 0 else 'FAILED'} in {time.time() - t0:.0f}s")
        if rc != 0:
            failed.append(name)

    print(f"\n{'=' * 72}")
    print("all phases passed" if not failed else f"failed: {', '.join(failed)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
