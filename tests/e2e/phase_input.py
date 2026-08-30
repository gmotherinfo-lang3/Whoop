"""Phase 8: typing on a phone, while the strap is streaming underneath.

The failure this guards against: a background refresh lands mid-sentence and
rebuilds the page, taking the half-written entry with it. On a phone that is
not an edge case -- glancing at a notification and coming back does it.
"""
from __future__ import annotations

import json, os, shutil, sys, time

sys.path.insert(0, os.path.dirname(__file__))
from harness import (CHROME, Client, OWNER_EMAIL, OWNER_PASSWORD, Proc, WORK,
                     check, jget, report, sign_in_browser, start_server,
                     wait_http)
from playwright.sync_api import sync_playwright

CFG = f"{WORK}/state7/config.toml"
PORT = 8451; LAN = f"http://127.0.0.1:{PORT}"
STATE = f"{WORK}/state7"; DB = f"{STATE}/whoop.db"
TODAY = time.strftime("%Y-%m-%d", time.gmtime())
shutil.rmtree(STATE, ignore_errors=True); os.makedirs(STATE)
open(CFG, "w").write(
    "[device]\naddress = \"AA:BB:CC:DD:EE:FF\"\n\n"
    f"[forward]\nforward_url = \"{LAN}/ingest\"\nforward_token = \"__PAIRED__\"\n")

srv = start_server(PORT, DB, log=f"{STATE}/server.log")
stream = None
pw = browser = None


def background_and_return(page):
    page.evaluate("""() => {
        Object.defineProperty(document, 'hidden', {value: true, configurable: true});
        document.dispatchEvent(new Event('visibilitychange'));
        Object.defineProperty(document, 'hidden', {value: false, configurable: true});
        document.dispatchEvent(new Event('visibilitychange'));
    }""")
    page.wait_for_timeout(2000)


try:
    assert wait_http(f"{LAN}/healthz")
    owner = Client(LAN)
    owner.sign_up_owner()
    open(CFG, "w").write(open(CFG).read().replace("__PAIRED__", owner.pair_a_laptop()))
    # Seed a little history so every tab has something to draw.
    seed = Proc([sys.executable, f"{WORK}/laptop.py",
                 "--config", CFG,
                 "--spool", f"{STATE}/seed.db", "--url", f"{LAN}/ingest",
                 "--backfill-minutes", "1440",
                 "--backfill-start", str(int(time.time()) - 86400),
                 "--stats", f"{STATE}/seed.json"], log=f"{STATE}/seed.log")
    seed.p.wait(timeout=400)
    check("history seeded", json.load(open(f"{STATE}/seed.json"))["produced"] == 1440)

    # Live records arriving underneath the whole phase.
    stream = Proc([sys.executable, f"{WORK}/laptop.py",
                   "--config", CFG,
                   "--spool", f"{STATE}/live.db", "--url", f"{LAN}/ingest",
                   "--live-seconds", "110", "--live-rate", "8",
                   "--stats", f"{STATE}/live.json"], log=f"{STATE}/live.log")
    time.sleep(3)

    pw = sync_playwright().start()
    browser = pw.chromium.launch(executable_path=CHROME)
    ctx = browser.new_context(**pw.devices["iPhone 13"])
    ph = ctx.new_page()
    errs = []
    ph.on("pageerror", lambda e: errs.append(str(e)))
    sign_in_browser(ph, LAN)
    ph.goto(LAN + "/", wait_until="networkidle")

    print("\n[phase 8] work in progress must survive a background refresh")
    ph.click('[data-tab="journal"]'); ph.wait_for_selector("#jnotes", timeout=20000)
    ph.wait_for_timeout(600)
    note = "walked the dog, felt rough after lunch, early night planned"
    ph.fill("#jnotes", note)
    ph.fill("#newtag", "padel"); ph.click("#addtag")
    ph.click('[data-tag="alcohol"]')
    check("the tag toggled on when tapped",
          ph.get_attribute('[data-tag="alcohol"]', "aria-pressed") == "true",
          "pressed=" + str(ph.get_attribute('[data-tag="alcohol"]', "aria-pressed")))

    background_and_return(ph)
    check("a half-written note survives leaving the app and coming back",
          ph.input_value("#jnotes") == note, repr(ph.input_value("#jnotes"))[:80])
    check("an unsaved new tag survives too",
          ph.locator('[data-tag="padel"][aria-pressed="true"]').count() == 1)
    check("an unsaved tag selection survives too",
          ph.locator('[data-tag="alcohol"][aria-pressed="true"]').count() == 1)

    # Now save, and the refresh should be free to resume.
    ph.click("#jsave"); ph.wait_for_timeout(1200)
    _, entry = owner.call(f"/api/journal/{TODAY}")
    check("saving stores everything that was typed",
          entry.get("notes") == note and set(entry.get("tags", [])) >= {"padel", "alcohol"},
          str(entry)[:180])

    ph.evaluate("window.__refreshed = 0; (function(){ const o = document.querySelector('#content');"
                "new MutationObserver(()=>window.__refreshed++).observe(o,{childList:true}); })()")
    background_and_return(ph)
    check("background refreshes resume once the work is saved",
          ph.evaluate("window.__refreshed") > 0,
          f"rebuilds={ph.evaluate('window.__refreshed')}")

    # The activity form is the other place with typing worth keeping.
    print("\n[phase 8b] the activity form too")
    ph.click('[data-tab="activities"]'); ph.wait_for_selector("#mnote", timeout=20000)
    ph.wait_for_timeout(600)
    ph.fill("#mnote", "five a side, hard game")
    ph.fill("#mstart", "19:00"); ph.fill("#mend", "20:15")
    background_and_return(ph)
    check("a half-filled activity form survives backgrounding",
          ph.input_value("#mnote") == "five a side, hard game"
          and ph.input_value("#mstart") == "19:00",
          f"note={ph.input_value('#mnote')!r} start={ph.input_value('#mstart')!r}")
    ph.click("#madd"); ph.wait_for_timeout(1500)
    _, acts = owner.call(f"/api/activities?date={TODAY}&detect=false")
    check("the activity saved with the values that were typed",
          any(a.get("note") == "five a side, hard game" for a in acts.get("activities", [])),
          str(acts)[:200])

    # A tab with no form must still refresh itself while data streams in.
    print("\n[phase 8c] tabs with nothing to type still refresh live")
    ph.click('[data-tab="today"]'); ph.wait_for_timeout(2500)
    first = ph.evaluate("document.querySelector('#updated')?.textContent || ''")
    ph.evaluate("window.__t = 0; new MutationObserver(()=>window.__t++)"
                ".observe(document.querySelector('#content'),{childList:true})")
    background_and_return(ph)
    check("the Today tab still refreshes on return",
          ph.evaluate("window.__t") > 0, f"rebuilds={ph.evaluate('window.__t')}")

    # The guard that defers a refresh must not also cancel a render already in
    # flight -- that would leave the skeleton on screen with nothing to replace
    # it. Fire a background refresh into the middle of a tab switch and check
    # that real content still arrives.
    print("\n[phase 8d] a background refresh landing mid-navigation")
    for tab, marker in [("journal", "#jnotes"), ("activities", "#madd"),
                        ("insights", ".card"), ("body", ".card"), ("today", ".card")]:
        ph.click(f'[data-tab="{tab}"]')
        ph.wait_for_timeout(40)                       # mid-fetch
        ph.evaluate("""() => {
            Object.defineProperty(document, 'hidden', {value: false, configurable: true});
            document.dispatchEvent(new Event('visibilitychange'));
        }""")
        ph.wait_for_selector(marker, timeout=20000)
        text = ph.evaluate("document.querySelector('#content').innerText.trim()")
        check(f"the {tab} tab still finishes rendering",
              len(text) > 40 and "Loading" not in text[:40], text[:70].replace("\n", " / "))

    check("no page errors throughout", not errs, "; ".join(errs[:3]))

    stream.p.wait(timeout=200)
    live = json.load(open(f"{STATE}/live.json"))
    check("records kept flowing the whole time",
          live["spool_depth"] == 0 and live["produced"] > 400,
          f"produced={live['produced']} depth={live['spool_depth']}")
finally:
    if browser: browser.close()
    if pw: pw.stop()
    for p in (stream, srv):
        if p:
            try: p.stop()
            except Exception: pass

sys.exit(report())
