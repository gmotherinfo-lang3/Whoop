"""Phase 9: motion and export -- the two things the user actually asked for.

Motion is only worth having if it never fires underneath someone. The failure
this guards against is an entrance animation that replays on every twenty
second background refresh, which is what makes a live dashboard feel glitchy
rather than alive. So the checks are as much about what does NOT animate as
about what does.

Export is checked the way a spreadsheet would read it: the first column is the
time, the rest are one metric each, every row the same width, and the file the
browser actually receives is the file the server meant to send.
"""
from __future__ import annotations

import csv
import io
import json
import os
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from harness import (CHROME, Client, Proc, WORK, check, report,
                     sign_in_browser, start_server, wait_http)
from playwright.sync_api import sync_playwright

PORT = 8461
LAN = f"http://127.0.0.1:{PORT}"
STATE = f"{WORK}/state8"
DB = f"{STATE}/whoop.db"
CFG = f"{STATE}/config.toml"

shutil.rmtree(STATE, ignore_errors=True)
os.makedirs(STATE)
open(CFG, "w").write(
    '[device]\naddress = "AA:BB:CC:DD:EE:FF"\n\n'
    f'[forward]\nforward_url = "{LAN}/ingest"\nforward_token = "__PAIRED__"\n')

srv = start_server(PORT, DB, log=f"{STATE}/server.log")
pw = browser = None
try:
    assert wait_http(f"{LAN}/healthz")
    owner = Client(LAN)
    owner.sign_up_owner()
    Path(CFG).write_text(Path(CFG).read_text().replace("__PAIRED__", owner.pair_a_laptop()))

    # Three days, so the charts have a line to draw and the export has rows
    # with a gap in them.
    seed = Proc([sys.executable, f"{WORK}/laptop.py", "--config", CFG,
                 "--spool", f"{STATE}/seed.db", "--url", f"{LAN}/ingest",
                 "--backfill-minutes", "4320",
                 "--backfill-start", str(int(time.time()) - 3 * 86400),
                 "--stats", f"{STATE}/seed.json"], log=f"{STATE}/seed.log")
    seed.p.wait(timeout=900)
    check("three days of history seeded",
          json.load(open(f"{STATE}/seed.json"))["produced"] == 4320)

    pw = sync_playwright().start()
    browser = pw.chromium.launch(executable_path=CHROME)
    ctx = browser.new_context(**pw.devices["iPhone 13"], accept_downloads=True)
    ph = ctx.new_page()
    errs: list[str] = []
    ph.on("pageerror", lambda e: errs.append(str(e)))
    sign_in_browser(ph, LAN)
    ph.goto(LAN + "/", wait_until="networkidle")
    ph.wait_for_selector(".ringarc", timeout=25000)

    # --- the rings must clear the phone's status bar -------------------------
    print("\n[phase 9] the ring bar sits below the status bar, not under it")
    ph.wait_for_timeout(1500)
    top = ph.evaluate("""() => {
        const r = document.querySelector('#ringbar');
        const s = getComputedStyle(r);
        return {top: r.getBoundingClientRect().top, sticky: s.position,
                stickTo: s.top};
    }""")
    check("the ring bar is sticky, not pinned to the raw viewport top",
          top["sticky"] == "sticky" and top["stickTo"] != "0px" or top["top"] > 0,
          str(top))
    ph.evaluate("window.scrollTo(0, 800)")
    ph.wait_for_timeout(400)
    stuck = ph.evaluate("document.querySelector('#ringbar').getBoundingClientRect().top")
    check("scrolled hard, the rings still stop at the safe area",
          stuck >= 0, f"top={stuck}")
    ph.evaluate("window.scrollTo(0, 0)")

    # --- motion --------------------------------------------------------------
    print("\n[phase 9b] the entrance animation runs once, not every refresh")
    ph.click('[data-tab="journal"]')
    ph.wait_for_selector("#jnotes", timeout=20000)
    ph.click('[data-tab="today"]')
    ph.wait_for_selector(".ringarc", timeout=20000)
    ph.wait_for_timeout(2000)
    settled = ph.evaluate("""() => [...document.querySelectorAll('.ringarc')]
        .map(a => Number(a.getAttribute('stroke-dashoffset')))""")
    check("every ring arc reaches its final offset",
          len(settled) >= 4 and all(v is not None for v in settled), str(settled)[:120])

    # Count up: the number is written from zero and lands on the real value.
    shown = ph.evaluate("""() => [...document.querySelectorAll('.rgv [data-count]')]
        .map(n => [n.dataset.count, n.textContent.trim()])""")
    check("each ring number ends on exactly the value it counted towards",
          bool(shown) and all(a == b for a, b in shown), str(shown))

    # The real test: force a quiet refresh and prove nothing re-animates.
    ph.evaluate("""() => {
        window.__anim = [];
        document.addEventListener('animationstart',
            e => window.__anim.push(e.animationName), true);
        window.__trans = [];
        document.addEventListener('transitionrun',
            e => window.__trans.push(e.propertyName), true);
    }""")
    ph.evaluate("render({quiet:true})")
    ph.wait_for_timeout(2500)
    anim = ph.evaluate("window.__anim")
    trans = ph.evaluate("window.__trans")
    check("a background refresh replays no entrance animations",
          not [a for a in anim if a in ("rise", "fadein")], str(anim)[:150])
    check("a background refresh does not redraw the ring arcs",
          "stroke-dashoffset" not in trans, str(trans)[:150])
    check("a background refresh does not redraw the chart lines",
          "clip-path" not in trans, str(trans)[:150])

    # A real navigation still animates -- otherwise the fix is just "no motion".
    ph.evaluate("window.__anim = []; window.__trans = []")
    ph.click('[data-tab="insights"]')
    ph.wait_for_selector(".card", timeout=20000)
    ph.wait_for_timeout(1200)
    check("a real navigation does animate",
          "rise" in ph.evaluate("window.__anim"),
          str(ph.evaluate("window.__anim"))[:150])

    # A value that changed during a quiet refresh gets flagged.
    print("\n[phase 9c] a number that moves during a refresh is highlighted")
    ph.click('[data-tab="today"]')
    ph.wait_for_selector(".ringarc", timeout=20000)
    ph.wait_for_timeout(1800)
    flagged = ph.evaluate("""async () => {
        const n = document.querySelector('.rgv [data-live]');
        if(!n) return 'no live node';
        // Pretend the previous refresh saw a different number, then run one.
        lastSeen.set(n.dataset.live, 'definitely-different');
        await render({quiet:true});
        await new Promise(r => setTimeout(r, 200));
        return document.querySelectorAll('.changed').length;
    }""")
    check("a changed value is highlighted after a quiet refresh",
          flagged == 1, str(flagged))

    unchanged = ph.evaluate("""async () => {
        await render({quiet:true});
        await new Promise(r => setTimeout(r, 300));
        return document.querySelectorAll('.changed').length;
    }""")
    check("a refresh that changed nothing highlights nothing", unchanged == 0,
          str(unchanged))

    # --- export --------------------------------------------------------------
    print("\n[phase 9d] export from the settings page")
    ph.evaluate("location.hash = '#/settings'")
    ph.wait_for_selector("#export", timeout=20000)
    ph.wait_for_timeout(500)

    with ph.expect_download(timeout=30000) as dl:
        ph.click("#export")
    got = dl.value
    path = f"{STATE}/{got.suggested_filename}"
    got.save_as(path)
    check("the daily export downloads with a name that says what it is",
          got.suggested_filename.startswith("whoop-daily-")
          and got.suggested_filename.endswith(".csv"), got.suggested_filename)

    table = list(csv.reader(io.StringIO(Path(path).read_text())))
    check("the first column is the time and the rest are metrics",
          table[0][0] == "Date" and "Recovery %" in table[0] and len(table[0]) > 8,
          str(table[0])[:160])
    check("thirty rows of days came back, oldest first",
          len(table) == 31 and table[1][0] < table[-1][0],
          f"rows={len(table) - 1} {table[1][0] if len(table) > 1 else ''}"
          f"..{table[-1][0]}")
    widths = {len(r) for r in table}
    check("every row is exactly as wide as the header", len(widths) == 1, str(widths))
    measured = [r for r in table[1:] if r[1] or r[3]]
    check("the seeded days carry numbers", len(measured) >= 3, f"{len(measured)} rows")

    # Minute grain, over a custom window.
    ph.select_option("#exdata", "minutes")
    ph.select_option("#exrange", "custom")
    ph.wait_for_timeout(200)
    check("picking custom dates reveals the date fields",
          ph.is_visible("#exfrom"), "hidden")
    day = time.strftime("%Y-%m-%d", time.localtime(time.time() - 86400))
    ph.fill("#exfrom", day)
    ph.fill("#exto", day)
    with ph.expect_download(timeout=60000) as dl2:
        ph.click("#export")
    got2 = dl2.value
    path2 = f"{STATE}/{got2.suggested_filename}"
    got2.save_as(path2)
    mins = list(csv.reader(io.StringIO(Path(path2).read_text())))
    check("the minute export names the window it covers",
          day in got2.suggested_filename, got2.suggested_filename)
    check("minute rows start with a timestamp and carry heart rate",
          mins[0][0] == "Time" and "Heart rate (bpm)" in mins[0], str(mins[0])[:140])
    stamps = [r[0] for r in mins[1:]]
    check("one row per reading, in time order",
          len(stamps) > 500 and stamps == sorted(stamps), f"{len(stamps)} rows")
    check("every timestamp falls on the day that was asked for",
          all(s.startswith(day) for s in stamps),
          f"{[s for s in stamps if not s.startswith(day)][:2]}")

    # Journal grain, and text that would break a naive CSV writer.
    messy = 'ate late, drank "two", then\nslept badly'
    owner.call(f"/api/journal/{day}", method="PUT",
               data={"tags": ["beer, wine"], "amounts": {"alcohol_units": 2.5},
                     "notes": messy})
    ph.select_option("#exdata", "journal")
    with ph.expect_download(timeout=30000) as dl3:
        ph.click("#export")
    dl3.value.save_as(f"{STATE}/journal.csv")
    jrows = list(csv.reader(io.StringIO(Path(f"{STATE}/journal.csv").read_text())))
    row = dict(zip(jrows[0], jrows[1]))
    check("a tag containing a comma survives the round trip",
          row["Tags"] == "beer, wine", repr(row.get("Tags")))
    check("a note containing quotes and a newline survives",
          row["Notes"] == 'ate late, drank "two", then slept badly', repr(row.get("Notes")))
    check("a journal amount becomes its own column",
          row.get("Alcohol units") == "2.50", str(jrows[0]))

    # --- the endpoint's own guard rails --------------------------------------
    print("\n[phase 9e] the export endpoint refuses what it cannot honour")
    code, _ = owner.call("/api/export.csv?dataset=everything&days=7", raw=True)
    check("an unknown dataset is refused", code == 400, str(code))
    code, _ = owner.call("/api/export.csv?dataset=daily", raw=True)
    check("no time frame at all is refused", code == 400, str(code))
    code, _ = owner.call("/api/export.csv?dataset=daily&start=2000-01-01"
                         "&end=2030-01-01", raw=True)
    check("a range longer than a year is refused", code == 400, str(code))
    code, body = owner.call("/api/export.csv?dataset=daily&start=2026-01-10"
                            "&end=2026-01-05", raw=True)
    check("dates the wrong way round still export the days between",
          code == 200 and len(body.decode().splitlines()) == 7,
          f"{code} {len(body.decode().splitlines())}")

    # And that it is yours alone.
    code, _ = Client(LAN).call("/api/export.csv?dataset=daily&days=7", raw=True)
    check("a signed-out browser cannot export anything", code in (401, 403),
          str(code))

    check("no page errors throughout", not errs, "; ".join(errs[:3]))
finally:
    if browser: browser.close()
    if pw: pw.stop()
    try: srv.stop()
    except Exception: pass

sys.exit(report())
