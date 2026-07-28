<div align="center">

<img src="app/web/static/favicon.svg" alt="Enklask" width="84" height="84" />

# Enklask

***Enklask*** — Breton for **“investigation / inquiry.”**
A fitting name for a tool that quietly investigates the marketplaces and reports what it digs up.

</div>

A self-hosted tracker that watches second-hand marketplaces for **whatever you're
hunting**. Define searches grouped into topics and it aggregates the matching ads
across **eBay, Vinted, Leboncoin and Facebook Marketplace** (France)
in a single dashboard — photos, price, price-drop history, location, and a one-click
link to the original ad.

It started as a Nintendo-Switch-repair-parts hunter around Rennes (job lots, "for
parts" consoles, motherboards, screens), but every search, topic and the home
location is configurable — track anything: a console, a bike, a camera lens, a couch.

> **Heads-up on data sources.** Only **eBay** offers an official API. The others
> (Vinted, Leboncoin, Facebook Marketplace) have **no public API**, so
> their adapters hit unofficial endpoints through a browser-TLS fingerprint
> (`curl_cffi`) — no real browser. They can break when the sites change, and using them
> may be against those sites' Terms of Service. Each source toggles independently, so
> the app stays useful even with eBay alone. This is a **personal-use** tool with
> conservative, low-volume polling — please use it responsibly.

## Features

- Unified feed of ads from every enabled marketplace, newest first.
- Automatic classification by **console model** (V1/V2/Lite/OLED) and **part type**
  (job lot, for parts, motherboard, chassis, screen, joycon, battery).
- Filters: source, model, part, max price, distance from home, shipping option, status.
- Distance to each ad computed from your home coordinates.
- "Like" and "Hide" actions; deep link to buy/like on the original site.
- Background scheduler polls on an interval; "Rafraîchir" button polls on demand.

## Source status

| Source              | Access                         | Status                          |
|---------------------|--------------------------------|---------------------------------|
| eBay                | Official Browse API            | ✅ Stable                       |
| Vinted              | Unofficial internal JSON API   | ✅ Works (may break on changes) |
| Leboncoin           | Unofficial API (browser-TLS)   | ✅ Works (no key)               |
| Facebook Marketplace| Public GraphQL (browser-TLS)    | ✅ Works (no account)           |

## Quick start (local)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # then fill in your eBay keys
uvicorn app.main:app --reload
```

Open http://localhost:8000.

### eBay credentials

1. Create a free account at https://developer.ebay.com/.
2. Make a **production** keyset; copy the **App ID (Client ID)** and **Cert ID (Client Secret)**.
3. Put them in `.env` as `EBAY_CLIENT_ID` / `EBAY_CLIENT_SECRET`. Keep
   `ENABLE_EBAY=true`. The default marketplace is `EBAY_FR`.

Without credentials the app still runs — the eBay source simply reports as disabled.

## Run with Docker (recommended for an always-on mini server)

**Prerequisites:** Docker Engine + Docker Compose v2 (or Docker Desktop on macOS/Windows).

**Boot it up** (works with zero config — no API keys needed to start):

```bash
cp .env.example .env          # required: Compose reads .env (it can stay all-default)
docker compose up -d --build  # build the image and start in the background
```

**Verify it's running:**

```bash
curl -s localhost:8000/healthz      # -> {"status":"ok"}
```

Then open <http://localhost:8000>. With no keys configured this is expected to show
**"Aucune source configurée"** and an empty feed — that's the correct boot-only state and
confirms the whole stack (API, dashboard, scheduler) is up.

**Operate:**

```bash
docker compose logs -f        # watch polling activity
docker compose down           # stop (data is kept)
```

The SQLite database persists in `./data/ad_tracker.db`, so your ads survive restarts.

**Turn on a real source** when ready: edit `.env` — e.g. add your eBay
`EBAY_CLIENT_ID` / `EBAY_CLIENT_SECRET`, or set `ENABLE_VINTED=true` / `ENABLE_FACEBOOK=true` —
then re-apply:

```bash
docker compose up -d --build
```

Works on a Raspberry Pi / small box — every source crawls over plain HTTP (no browser).

## Configuration

All settings live in `.env` (see `.env.example`). Highlights:

- `POLL_INTERVAL_MINUTES` — how often to poll (default 10).
- `HOME_LAT` / `HOME_LON` — your location for distance (default Rennes).
- `ENABLE_EBAY` / `ENABLE_VINTED` / `ENABLE_LEBONCOIN` / `ENABLE_FACEBOOK` — per-source toggles.

Default saved searches (lot / pour pièces, carte mère, châssis, écran OLED, Lite HS) are
seeded on first run.

## Notifications (Apprise)

Get pushed when a **new** ad appears, anywhere you like. The app uses
[Apprise](https://github.com/caronc/apprise/wiki), so one or more destinations are
configured as URL strings in `APPRISE_URLS` (comma or space separated):

```bash
# ntfy (free, no account — great on a phone)
APPRISE_URLS=ntfy://ntfy.sh/mon-sujet-switch
# Telegram bot
APPRISE_URLS=tgram://123456:ABC-DEF.../987654321
# Several at once
APPRISE_URLS=ntfy://ntfy.sh/switch, discord://id/token
```

Each new ad sends one push (title with source + price, body with model/part tags,
location/distance, and the direct link; the photo is attached when available).

Flood guards:
- **First run is silent.** When the database is empty, that poll just seeds it — no
  alerts. Set `NOTIFY_ON_FIRST_RUN=true` to override.
- **Big batches digest.** If a single poll finds more than `NOTIFY_MAX_PER_POLL` (15)
  new ads, you get one summary message instead of a flood.
- Delivery is best-effort and isolated — a bad URL or a failing service is logged and
  never interrupts polling.

Verify your setup any time:

```bash
curl -X POST http://localhost:8000/api/notify/test
# -> {"enabled": true, "sent": true}
```

## Tests

```bash
pip install pytest
pytest
```

Covers the taxonomy classifier, dedup/upsert logic, and the eBay response parser
(no live network needed).

### Enabling the unofficial sources

- **Vinted** — set `ENABLE_VINTED=true`. No credentials; cookies are bootstrapped
  automatically. If it stops returning results, Vinted likely changed its internal API.
- **Leboncoin** — set `ENABLE_LEBONCOIN=true`. Uses `curl_cffi` to impersonate a browser's
  TLS fingerprint and self-mint a DataDome cookie, then queries the JSON search API — no
  browser. Needs a reasonably trusted (French residential) egress IP; from a blocked IP it
  reports an error and the other sources keep working.
- **Facebook Marketplace** — set `ENABLE_FACEBOOK=true`. Works **logged out, no account**: it
  harvests the `lsd` token + GraphQL `doc_id` from the public search page (browser-TLS via
  `curl_cffi`) and replays the Marketplace search around `HOME_LAT`/`HOME_LON`. Most
  ToS-sensitive source, and the `doc_id` rotates with FB deploys (re-harvested each search) —
  use sparingly.

## Roadmap

- Saved-search management UI in the dashboard.
- Per-notification-target routing (e.g. only alert on OLED motherboards under 30€).
