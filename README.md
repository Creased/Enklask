# 🎮 Switch Parts Tracker

A self-hosted tracker that watches second-hand marketplaces for **Nintendo Switch
parts** — job lots, "for parts" consoles, motherboards, chassis, screens — across
**eBay, Vinted, Leboncoin and Facebook Marketplace** (France), and shows them in a
single dashboard with photos, price, location and a one-click link to the original ad.

Built for Switch repair hunting (V1 / V2 / Lite / OLED) around Rennes, but the home
location and searches are configurable.

> **Heads-up on data sources.** Only **eBay** offers an official API. Vinted,
> Leboncoin and Facebook Marketplace have **no public API**, so those adapters use
> unofficial endpoints / a headless browser. They can break when the sites change and
> using them may be against those sites' Terms of Service. Each source can be toggled
> independently, so the app stays useful even with eBay alone. This is a **personal-use**
> tool with conservative, low-volume polling — please use it responsibly.

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
| Leboncoin           | API + Playwright fallback      | ⚠️ Best effort (DataDome)       |
| Facebook Marketplace| Headless browser (logged-in)   | 🧪 Experimental (opt-in)        |

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

```bash
cp .env.example .env        # fill in keys
docker compose up -d --build
```

The SQLite database persists in `./data`. Works on a Raspberry Pi / small box for the
API-based sources. The browser-based sources (Leboncoin fallback, Facebook) need
Playwright and are best run on x86 — see `requirements-scrapers.txt`.

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
- **Leboncoin** — set `ENABLE_LEBONCOIN=true`. Tries the JSON API first; if DataDome
  blocks it and the scraper deps are installed (`requirements-scrapers.txt` +
  `playwright install chromium`), it automatically falls back to a headless browser that
  reads ads from the page's embedded `__NEXT_DATA__`. If both are blocked, it reports an
  error and the other sources keep working.
- **Facebook Marketplace** — set `ENABLE_FACEBOOK=true`, install the scraper deps
  (`pip install -r requirements-scrapers.txt && playwright install chromium`), and export a
  logged-in session to the path in `FACEBOOK_STORAGE_STATE` (a Playwright `storage_state`
  JSON). Off until that file exists. Most fragile / ToS-sensitive — use sparingly.

## Roadmap

- Saved-search management UI in the dashboard.
- Per-notification-target routing (e.g. only alert on OLED motherboards under 30€).
