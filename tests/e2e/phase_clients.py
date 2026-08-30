"""Phase 6: the laptop and the iPhone, at the same time, while data streams in.

The laptop reads over the LAN. The phone goes through the tunnel with an
Access session, and does the writing -- journal, activities, intake -- because
that is what the phone is actually for.
"""
from __future__ import annotations

import io
import json
import os
import shutil
import sys
import threading
import time
import zipfile

sys.path.insert(0, os.path.dirname(__file__))
from harness import (CHROME, Client, INGEST_TOKEN, OWNER_EMAIL, Proc,
                     SERVICE_ID, SERVICE_SECRET, WORK, check, http, jget,
                     report, sign_in_browser, start_server, start_tunnel, wait_http)
from playwright.sync_api import sync_playwright

PORT, TUNNEL_PORT = 8421, 8422
LAN, TUNNEL = f"http://127.0.0.1:{PORT}", f"http://127.0.0.1:{TUNNEL_PORT}"
STATE = f"{WORK}/state3"
DB = f"{STATE}/whoop.db"
SVC = {"CF-Access-Client-Id": SERVICE_ID, "CF-Access-Client-Secret": SERVICE_SECRET}
TODAY = time.strftime("%Y-%m-%d", time.gmtime())

shutil.rmtree(STATE, ignore_errors=True)
os.makedirs(f"{STATE}/laptop", exist_ok=True)

srv = start_server(PORT, DB, log=f"{STATE}/server.log")
tun = start_tunnel(TUNNEL_PORT, LAN, log=f"{STATE}/tunnel.log")
bridge = None
pw = browser = None
try:
    assert wait_http(f"{LAN}/healthz"), "server down"
    assert wait_http(f"{TUNNEL}/healthz", cookie="CF_Authorization=valid-session")

    owner = Client(LAN)
    owner.sign_up_owner()
    device_token = owner.pair_a_laptop("e2e laptop")
    body = f"cf_access_client_id={SERVICE_ID}&cf_access_client_secret={SERVICE_SECRET}".encode()
    _, zdata, _ = http(f"{TUNNEL}/setup/bundle.zip", method="POST", data=body,
                       cookie="CF_Authorization=valid-session",
                       headers={"Content-Type": "application/x-www-form-urlencoded"})
    zipfile.ZipFile(io.BytesIO(zdata)).extractall(f"{STATE}/laptop")
    root = f"{STATE}/laptop"
    while len(os.listdir(root)) == 1 and os.path.isdir(f"{root}/{os.listdir(root)[0]}"):
        root = f"{root}/{os.listdir(root)[0]}"
    cfg_path = f"{root}/config.toml"
    _cfg = open(cfg_path).read().replace('forward_token = ""',
                                         f'forward_token = "{device_token}"')
    open(cfg_path, "w").write(_cfg)

    # Seed enough history that the views have something to say, then leave a
    # live stream running underneath the browsers for the rest of the phase.
    print("\n[phase 6] seeding history, then streaming live under both clients")
    seed = Proc([sys.executable, f"{WORK}/laptop.py", "--config", cfg_path,
                 "--spool", f"{STATE}/spool.db", "--url", f"{TUNNEL}/ingest",
                 "--backfill-minutes", "2880",
                 "--backfill-start", str(int(time.time()) - 2 * 86400),
                 "--stats", f"{STATE}/seed.json"], log=f"{STATE}/seed.log")
    seed.p.wait(timeout=600)
    check("history seeded through the tunnel",
          json.load(open(f"{STATE}/seed.json"))["produced"] == 2880)

    bridge = Proc([sys.executable, f"{WORK}/laptop.py", "--config", cfg_path,
                   "--spool", f"{STATE}/spool2.db", "--url", f"{TUNNEL}/ingest",
                   "--live-seconds", "150", "--live-rate", "6",
                   "--stats", f"{STATE}/live.json"], log=f"{STATE}/live.log")
    time.sleep(3)

    pw = sync_playwright().start()
    browser = pw.chromium.launch(executable_path=CHROME)

    laptop = browser.new_context(viewport={"width": 1280, "height": 900})
    iphone = browser.new_context(**pw.devices["iPhone 13"])
    iphone.add_cookies([{"name": "CF_Authorization", "value": "valid-session",
                         "domain": "127.0.0.1", "path": "/"}])

    errs: list[str] = []
    bad_http: list[str] = []

    def watch(page, who):
        page.on("pageerror", lambda e: errs.append(f"{who}: {e}"))
        page.on("response", lambda r: bad_http.append(f"{who}: {r.status} {r.url}")
                if r.status >= 400 or r.status == 302 else None)

    lp = laptop.new_page(); watch(lp, "laptop")
    ph = iphone.new_page(); watch(ph, "phone")

    sign_in_browser(lp, LAN)
    sign_in_browser(ph, TUNNEL)
    lp.goto(LAN + "/", wait_until="networkidle")
    ph.goto(TUNNEL + "/", wait_until="networkidle")
    ph.wait_for_timeout(2500)

    check("the phone loads the whole app through the tunnel",
          ph.locator("nav button").count() == 6 and ph.locator("#content").count() == 1)
    check("no request inside the phone page was bounced or errored",
          not bad_http, "; ".join(bad_http[:4]))

    # --- the phone writes a journal entry ----------------------------------
    print("\n[phase 6a] the phone writes; the laptop reads")
    ph.click('[data-tab="journal"]'); ph.wait_for_selector("#tags", timeout=20000)
    ph.fill("#newtag", "padel"); ph.click("#addtag")
    ph.click('[data-tag="alcohol"]')
    ph.fill("#jnotes", "logged from the phone on the train")
    ph.click("#jsave"); ph.wait_for_timeout(1500)

    status, entry = owner.call(f"/api/journal/{TODAY}")
    check("the phone's journal entry reached the server",
          status == 200 and "alcohol" in entry.get("tags", []) and
          "padel" in entry.get("tags", []) and "train" in entry.get("notes", ""),
          str(entry)[:200])

    lp.click('[data-tab="journal"]'); lp.wait_for_selector("#jnotes", timeout=20000)
    lp.wait_for_timeout(800)
    check("the laptop shows what the phone wrote",
          "train" in (lp.input_value("#jnotes") or "") and
          lp.locator('[data-tag="alcohol"][aria-pressed="true"]').count() == 1,
          repr(lp.input_value("#jnotes"))[:100])

    # --- the phone adds, edits and deletes an activity ---------------------
    print("\n[phase 6b] the phone manages activities")
    ph.click('[data-tab="activities"]'); ph.wait_for_selector("#madd", timeout=20000)
    ph.select_option("#mtype", index=1)
    ph.fill("#mstart", "07:15"); ph.fill("#mend", "08:05")
    ph.fill("#mnote", "added on the phone")
    ph.click("#madd"); ph.wait_for_timeout(1500)

    status, acts = owner.call(f"/api/activities?date={TODAY}&detect=false")
    mine = [a for a in acts.get("activities", []) if a.get("note") == "added on the phone"]
    check("the phone's activity was stored", len(mine) == 1, str(acts)[:200])

    if mine:
        aid = mine[0]["id"]
        # Through the tunnel, so both credentials are needed: Cloudflare's at
        # the edge and the app's session behind it.
        phone_api = Client(TUNNEL)
        phone_api.cookie = "CF_Authorization=valid-session; " + owner.cookie

        status, _ = phone_api.call(f"/api/activities/{aid}", method="PATCH",
                                   data={"note": "edited on the phone"})
        _, acts = owner.call(f"/api/activities?date={TODAY}&detect=false")
        edited = [a for a in acts["activities"] if a["id"] == aid]
        check("editing from the phone works through the tunnel",
              status == 200 and edited and edited[0]["note"] == "edited on the phone",
              f"status={status} {str(edited)[:130]}")

        status, _ = phone_api.call(f"/api/activities/{aid}", method="DELETE")
        _, acts = owner.call(f"/api/activities?date={TODAY}&detect=false")
        live = [a for a in acts["activities"] if a["id"] == aid and not a.get("deleted")]
        check("deleting from the phone works", status == 200 and not live,
              f"status={status} {str(acts)[:130]}")

    # --- intake from the phone ---------------------------------------------
    print("\n[phase 6c] the phone logs caffeine and alcohol")
    ph.click('[data-tab="journal"]'); ph.wait_for_selector("#iadd", timeout=20000)
    ph.select_option("#isub", index=0); ph.fill("#itime", "09:30")
    ph.click("#iadd"); ph.wait_for_timeout(1200)
    status, intake = owner.call(f"/api/intake?date={TODAY}")
    check("intake logged from the phone is stored",
          status == 200 and len(intake.get("entries", [])) >= 1, str(intake)[:200])

    # --- both clients on the detail views while data streams ---------------
    print("\n[phase 6d] both clients on live views at once")
    for name, tab in [("health", "today"), ("stress", "today"), ("age", "body")]:
        for page, who in ((lp, "laptop"), (ph, "phone")):
            page.click(f'[data-tab="{tab}"]'); page.wait_for_timeout(500)
            page.click(f'[data-open="{name}"]')
            page.wait_for_selector(".dhead", timeout=25000)
            txt = page.evaluate("document.querySelector('#content').innerText.trim()")
            check(f"{who}: {name} detail renders under live data",
                  len(txt) > 120 and "Could not load" not in txt, txt[:80].replace("\n", " / "))
            page.click("#dback"); page.wait_for_timeout(400)

    # --- the phone is a phone ----------------------------------------------
    print("\n[phase 6e] the phone layout")
    ph.click('[data-tab="today"]'); ph.wait_for_timeout(2000)
    overflow = ph.evaluate("""() => {
        const d = document.documentElement;
        const wide = [...document.querySelectorAll('body *')]
          .filter(e => e.getBoundingClientRect().right > d.clientWidth + 1)
          .map(e => e.tagName + '.' + (e.className || '').toString().slice(0, 30));
        return {scrollW: d.scrollWidth, clientW: d.clientWidth, wide: wide.slice(0, 5)};
    }""")
    check("nothing overflows the phone screen sideways",
          overflow["scrollW"] <= overflow["clientW"] + 1, str(overflow))

    small = ph.evaluate("""() => [...document.querySelectorAll('nav button, .btn, .chip')]
        .map(e => { const r = e.getBoundingClientRect();
                    return {t: e.textContent.trim().slice(0,14), h: Math.round(r.height),
                            w: Math.round(r.width)}; })
        .filter(x => x.h > 0 && (x.h < 30 || x.w < 30))""")
    check("tap targets are big enough for a thumb", not small, str(small[:4]))

    # --- did anything break while all this happened? -----------------------
    check("no page errors on either client", not errs, "; ".join(errs[:4]))
    check("no failed requests on either client", not bad_http, "; ".join(bad_http[:4]))

    bridge.p.wait(timeout=200)
    live = json.load(open(f"{STATE}/live.json"))
    check("the bridge streamed throughout without losing anything",
          live["spool_depth"] == 0 and live["produced"] > 500,
          f"produced={live['produced']} depth={live['spool_depth']}")
finally:
    if browser: browser.close()
    if pw: pw.stop()
    for p in (bridge, tun, srv):
        if p:
            try: p.stop()
            except Exception: pass

sys.exit(report())
