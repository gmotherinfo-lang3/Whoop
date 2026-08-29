# Deployment

Three pieces:

```
WHOOP 4.0  --BLE-->  Windows laptop        server (Docker)          your phone
                     tray app + bridge  ->  FastAPI + SQLite  <--   browser
                     (collector only)       + dashboard              via tunnel
                                                  ^                     |
                                                  +--- Cloudflare Tunnel +
                                                       + Access (auth)
```

The laptop only collects and forwards. All storage, metrics and UI live on the
server, so the laptop and the phone open the *same* dashboard.

---

## 1. Server

```bash
cp .env.example .env
python -c "import secrets; print(secrets.token_urlsafe(32))"   # -> INGEST_TOKEN
```

Fill in `.env`:

- `INGEST_TOKEN` — the secret the bridge posts with.
- `TUNNEL_TOKEN` — from Cloudflare Zero Trust → Networks → Tunnels → your
  tunnel → Install connector (copy the token out of the shown command).
- `MAX_HR` — **set this**. Strain is computed against it; the 190 default is
  the `220 − age` rule at age 30 and will skew your numbers if that is not you.
- `TZ_OFFSET_HOURS` — so "days" break at your local midnight, not UTC.

```bash
docker compose up -d --build
curl -s localhost:8000/healthz
```

The app binds to `127.0.0.1` only. Nothing reaches it from outside except
through the tunnel.

## 2. Tunnel and Access

In the Cloudflare Zero Trust dashboard:

1. **Tunnels** → your tunnel → **Public hostname**: route e.g.
   `whoop.yourdomain.com` → `http://whoop:8000` (the compose service name).
2. **Access → Applications** → *Add a self-hosted application* for that
   hostname. Policy: *Allow*, with an **Emails** rule listing your own address.
   Free tier covers up to 50 users; login is an emailed one-time PIN.

Now the phone gets an Access login before it ever reaches your data.

### The bridge cannot log in like a browser

This is the part that trips people up. Once Access is on, your laptop's POSTs
get a **302 redirect to the login page**, not a 200 — the bridge is not a
browser and cannot complete an interactive login. Pick one:

**A. Service token (recommended if laptop and server are on different networks)**

Zero Trust → **Access → Service Auth** → create a service token. Add a second
policy on the application: action *Service Auth*, rule *Service Token* → the
one you made. Then in `config.toml`, or better as env vars:

```powershell
$env:CF_ACCESS_CLIENT_ID     = "….access"
$env:CF_ACCESS_CLIENT_SECRET = "…"
```

The bridge sends these as `CF-Access-Client-Id` / `CF-Access-Client-Secret`.
If it gets a 302 or 403 without them set, the log says so explicitly.

**B. Post over the LAN (simplest if they share a network)**

Point `forward_url` at the server's local address instead of the public
hostname, so ingest never traverses Cloudflare:

```toml
forward_url = "https://whoop.lan:8000/ingest"
```

This needs a certificate the laptop trusts. Access still protects the public
hostname your phone uses. Do **not** set `verify_tls = false` to dodge a
certificate error — that removes the protection the HTTPS requirement exists
for.

Either way `INGEST_TOKEN` is still checked by the app itself, so Access is
defence in depth rather than the only lock.

## 3. Laptop

**Open `https://whoop.yourdomain.com/setup` in a browser on the laptop.** The
server hands you a zip of the bridge with `config.toml` already filled in with
its own URL and ingest token — there is nothing to copy across by hand. If you
use Access, paste a service token into the form first and it is baked in too.

Unzip it and run, in that folder:

```powershell
powershell -ExecutionPolicy Bypass -File windows\setup.ps1
.\.venv\Scripts\whoop-bridge.exe scan
```

The only thing still missing is your strap's address — paste what `scan` prints
into `address` in `config.toml`. Then:

```powershell
.\.venv\Scripts\whoop-bridge.exe test-endpoint
.\.venv\Scripts\pip.exe install -e ".[tray]"
.\.venv\Scripts\pythonw.exe -m tray.whoop_tray
```

The bundle repeats these steps in its own `START-HERE.md`.

### About that download

It contains a live credential — the ingest token, which can write to your
server — so `/setup` is gated. It is served only to a request Cloudflare Access
has authenticated, or to a client on a private/LAN address. A request arriving
through the tunnel without an Access login gets a 403.

That check reads headers set by cloudflared, which is sound **only because the
app binds to `127.0.0.1`** and nothing else can reach it. Do not publish port
8000 directly. `SETUP_DOWNLOAD=off` disables the endpoint entirely;
`SETUP_DOWNLOAD=open` removes the check and should never be used on the
internet.

Setting up over the LAN instead? The generated config will use `http://` to a
private address, which the bridge accepts (and logs a warning about). Plain
HTTP to a *public* host is still refused.

To start the tray at logon, put a shortcut to that last command in
`shell:startup`. For headless operation instead, use
`windows\install-task.ps1` as before.

**Un-pair the strap from the WHOOP phone app first.** It bonds to one host at a
time and the laptop will not see it otherwise.

## 4. Check it end to end

```bash
curl -s https://whoop.yourdomain.com/healthz \
  -H "CF-Access-Client-Id: …" -H "CF-Access-Client-Secret: …"
```

`records` should climb after the first sync. The tray icon shows green when
connected, amber while searching or when the endpoint is backed up.

## Backups

Everything is in the `whoop-data` Docker volume:

```bash
docker compose exec -T whoop sh -c 'cat /data/whoop.db' > whoop-backup-$(date +%F).db
```

The raw frame bytes are stored alongside the decoded values, so if a field
mapping is ever corrected you can re-decode history rather than re-collect it.

## Notes and limits

- **Early days look odd.** Recovery compares against a rolling baseline, so
  until there are a couple of weeks of history the score leans on whatever
  components exist. It settles as history accumulates.
- **The laptop is a single point of collection.** While it is off or out of
  range, the strap keeps recording (~14 days of storage) and the bridge pulls
  the backlog on the next connect. Nothing is lost, it just arrives late.
- **An iPhone could technically be a BLE bridge**, but iOS suspends background
  apps aggressively, so syncs would be sporadic. The always-on laptop is the
  better collector; the phone is a viewer.
