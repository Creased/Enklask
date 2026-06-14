"""eBay source adapter.

Two modes:
- **API mode** (keys set): uses the official Browse API with client-credentials
  OAuth.  Docs: https://developer.ebay.com/api-docs/buy/browse/overview.html
- **Scrape mode** (no keys): parses the public eBay search results page.
  Best-effort, no credentials required.
"""

from __future__ import annotations

import base64
import logging
import random
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from html import unescape
from urllib.parse import quote_plus

import httpx

from ..config import get_settings
from ..cookies import load_cookies
from ..enums import Source
from .base import BaseSource, RawListing, SearchQuery

try:  # curl_cffi impersonates a browser's TLS fingerprint -> past eBay's bot check.
    from curl_cffi import requests as cffi_requests

    HAVE_CURL_CFFI = True
except ImportError:  # pragma: no cover
    HAVE_CURL_CFFI = False

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

_OAUTH_URL = "https://api.ebay.com/identity/v1/oauth2/token"
_SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
_SCOPE = "https://api.ebay.com/oauth/api_scope"

# Desktop browser TLS profiles curl_cffi can impersonate (rotated on a challenge).
_IMPERSONATIONS = ["chrome", "chrome131", "edge", "safari", "firefox"]

_BROWSE_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
              "image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-User": "?1",
}
_MAX_FETCH_ATTEMPTS = 3
# When eBay hard-blocks the IP, back off this long before trying again instead of
# burning retries on every search (fast failure + lets the IP cool down).
_COOLDOWN_SECONDS = 120.0

# Per-domain eBay browser state. Each base URL keeps its own trusted session (its
# cookies + the TLS profile that last worked) and its own cooldown, so a returning
# visitor is reused across searches (eBay challenges far less) and a blocked
# www.ebay.fr never suppresses the www.ebay.com fallback. Guarded by a lock since
# FastAPI runs sync endpoints in a thread pool and curl_cffi sessions aren't
# thread-safe.
_ebay_state: dict = {}
_ebay_lock = threading.Lock()


def _domain_state(base: str) -> dict:
    return _ebay_state.setdefault(base, {"session": None, "cooldown_until": 0.0})


def _close_session(st: dict) -> None:
    sess = st.get("session")
    if sess is not None:
        try:
            sess.close()
        except Exception:  # noqa: BLE001
            pass
    st["session"] = None


def _inject_cookies(session, cookies_file: str, base: str) -> int:
    """Seed the session with exported cookies for *base*'s domain.

    Lets a bot-check solved in a real browser (and exported via cookies.txt) carry
    over to the crawler, the same way the Leboncoin datadome cookie does.
    """
    domain = base.split("//", 1)[-1]
    count = 0
    for cookie in load_cookies(cookies_file, domain).jar:
        try:
            session.cookies.set(cookie.name, cookie.value, domain=cookie.domain)
            count += 1
        except Exception:  # noqa: BLE001
            pass
    return count

_MARKETPLACE_DOMAINS = {
    "EBAY_FR": "www.ebay.fr",
    "EBAY_DE": "www.ebay.de",
    "EBAY_GB": "www.ebay.co.uk",
    "EBAY_US": "www.ebay.com",
    "EBAY_IT": "www.ebay.it",
    "EBAY_ES": "www.ebay.es",
}

# Generic sort -> eBay _sop (no native oldest; falls back to newly listed).
_SOP = {
    "relevance": "12",   # best match
    "recent": "10",      # newly listed
    "oldest": "10",
    "price_asc": "15",   # price + shipping: lowest first
    "price_desc": "16",  # price + shipping: highest first
}

# Generic condition -> eBay LH_ItemCondition.
_CONDITION = {
    "new": "1000",
    "like_new": "1500",   # open box
    "good": "3000",       # used
    "fair": "3000",
    "for_parts": "7000",  # for parts or not working
}

# ---- HTML parsing helpers (eBay "s-card" search markup) ---------------------
# The raw server HTML (what curl_cffi fetches) minifies single-token attribute
# values WITHOUT quotes (data-listingid=123, href=https://…, class=s-card__title)
# while multi-token ones stay quoted, so the value patterns allow optional quotes.

_CARD_OPEN_RE = re.compile(
    r'<li\b[^>]*\bclass="[^"]*\bs-card\b[^"]*"[^>]*>', re.IGNORECASE
)
_LISTING_ID_RE = re.compile(r'data-listingid=["\']?(\d+)')
_LINK_RE = re.compile(
    r'<a\b[^>]*\bclass="[^"]*s-card__link[^"]*"[^>]*\bhref=["\']?([^"\'\s>]+)',
    re.IGNORECASE,
)
# eBay puts a "Nouvelle annonce"/"New listing" badge span before the title span,
# so the image alt text is the reliable product title.
_ALT_RE = re.compile(r'\balt="([^"]+)"', re.IGNORECASE)
_TITLE_RE = re.compile(
    r'class=["\']?s-card__title["\']?[^>]*>\s*<span[^>]*>(.*?)</span>',
    re.IGNORECASE | re.DOTALL,
)
_EBAYIMG_RE = re.compile(r'https://i\.ebayimg\.com/[^\s"\'>]+', re.IGNORECASE)
_PRICE_RE = re.compile(r's-card__price["\']?\s*>(.*?)</span>', re.IGNORECASE | re.DOTALL)
_PRICE_VALUE_RE = re.compile(r"([\d]+(?:[.,]\d+)?)")
_TAG_RE = re.compile(r"<[^>]+>")

_SKIP_TITLES = {
    "shop on ebay", "annonce sponsorisée", "sponsored", "nouvelle annonce", "",
}

# Each card carries its listing date in the site's local time, without a year
# (e.g. "Jun-14 12:22"). Map each domain to its display timezone so the date can
# be normalized to naive UTC like the other sources.
_EBAY_TZ_BY_DOMAIN = {
    "www.ebay.com": "America/Los_Angeles",
    "www.ebay.fr": "Europe/Paris",
    "www.ebay.de": "Europe/Berlin",
    "www.ebay.co.uk": "Europe/London",
    "www.ebay.it": "Europe/Rome",
    "www.ebay.es": "Europe/Madrid",
}

_EBAY_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    # French month abbreviations (ebay.fr)
    "janv": 1, "févr": 2, "mars": 3, "avr": 4, "mai": 5, "juin": 6,
    "juil": 7, "août": 8, "aout": 8, "sept": 9, "déc": 12,
}
# Only matches real month tokens followed by a day ("Jun-14"), optionally a time.
_EBAY_DATE_RE = re.compile(
    r"\b(" + "|".join(sorted(_EBAY_MONTHS, key=len, reverse=True))
    + r")-(\d{1,2})(?:\s+(\d{1,2}):(\d{2}))?",
    re.IGNORECASE,
)


def _domain_tz(domain: str):
    name = _EBAY_TZ_BY_DOMAIN.get(domain)
    if name and ZoneInfo is not None:
        try:
            return ZoneInfo(name)
        except Exception:  # noqa: BLE001
            return None
    return None


def _parse_listing_date(card: str, tz) -> datetime | None:
    """Parse eBay's card date ("Jun-14 12:22") into naive UTC.

    The markup carries no year, so assume the current one and roll back a year if
    that lands in the future. The clock is read in the site's timezone (*tz*).
    """
    m = _EBAY_DATE_RE.search(card)
    if not m:
        return None
    month = _EBAY_MONTHS.get(m.group(1).lower())
    if not month:
        return None
    day = int(m.group(2))
    hour = int(m.group(3)) if m.group(3) else 0
    minute = int(m.group(4)) if m.group(4) else 0
    now = datetime.now(timezone.utc)
    try:
        dt = datetime(now.year, month, day, hour, minute, tzinfo=tz or timezone.utc)
    except ValueError:
        return None
    if dt > now + timedelta(days=1):
        dt = dt.replace(year=now.year - 1)
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


class EbaySource(BaseSource):
    name = Source.EBAY

    def __init__(self) -> None:
        self._settings = get_settings()
        self._token: str | None = None
        self._token_expiry: float = 0.0

    @property
    def enabled(self) -> bool:
        return bool(self._settings.enable_ebay)

    @property
    def _has_api_keys(self) -> bool:
        return bool(self._settings.ebay_client_id and self._settings.ebay_client_secret)

    # -- dispatch --------------------------------------------------------------

    def search(self, query: SearchQuery) -> list[RawListing]:
        if self._has_api_keys:
            return self._search_api(query)
        return self._search_scrape(query)

    # -- API mode (official Browse API) ----------------------------------------

    def _get_token(self) -> str:
        if self._token and time.time() < self._token_expiry - 60:
            return self._token

        creds = f"{self._settings.ebay_client_id}:{self._settings.ebay_client_secret}"
        basic = base64.b64encode(creds.encode()).decode()
        resp = httpx.post(
            _OAUTH_URL,
            headers={
                "Authorization": f"Basic {basic}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={"grant_type": "client_credentials", "scope": _SCOPE},
            timeout=20.0,
        )
        resp.raise_for_status()
        payload = resp.json()
        self._token = payload["access_token"]
        self._token_expiry = time.time() + int(payload.get("expires_in", 7200))
        return self._token

    def _search_api(self, query: SearchQuery) -> list[RawListing]:
        token = self._get_token()
        params: dict[str, str] = {
            "q": query.query,
            "limit": "100",
            "sort": "newlyListed",
        }
        filters = ["deliveryCountry:FR"]
        if query.price_max:
            filters.append(f"price:[..{query.price_max}]")
            filters.append("priceCurrency:EUR")
        # Browse API uses the same numeric condition codes as the scrape's
        # LH_ItemCondition (e.g. 7000 = "for parts or not working").
        if query.condition and query.condition in _CONDITION:
            filters.append(f"conditionIds:{{{_CONDITION[query.condition]}}}")
        params["filter"] = ",".join(filters)

        resp = httpx.get(
            _SEARCH_URL,
            params=params,
            headers={
                "Authorization": f"Bearer {token}",
                "X-EBAY-C-MARKETPLACE-ID": self._settings.ebay_marketplace_id,
                "Content-Type": "application/json",
            },
            timeout=30.0,
        )
        resp.raise_for_status()
        items = resp.json().get("itemSummaries", []) or []
        return [self._api_to_raw(item) for item in items]

    def _api_to_raw(self, item: dict) -> RawListing:
        price = item.get("price", {}) or {}
        image = item.get("image", {}) or {}
        thumb = image.get("imageUrl")
        photos = [thumb] if thumb else []
        for extra in item.get("additionalImages", []) or []:
            url = extra.get("imageUrl")
            if url:
                photos.append(url)

        loc = item.get("itemLocation", {}) or {}
        posted_at = _parse_date(item.get("itemCreationDate"))

        return RawListing(
            source=Source.EBAY,
            source_id=str(item.get("itemId", "")),
            title=item.get("title", ""),
            url=item.get("itemWebUrl", ""),
            price=_to_float(price.get("value")),
            currency=price.get("currency", "EUR"),
            thumbnail=thumb,
            photos=photos,
            location_city=loc.get("city") or loc.get("country"),
            posted_at=posted_at,
        )

    # -- Scrape mode (public search page, no keys) -----------------------------

    def _search_scrape(self, query: SearchQuery) -> list[RawListing]:
        domain = _MARKETPLACE_DOMAINS.get(
            self._settings.ebay_marketplace_id, "www.ebay.fr"
        )
        results = self._scrape_domain(domain, query, prefer_local=True)
        # eBay's localized domains (e.g. ebay.fr) are frequently bot-blocked while
        # www.ebay.com stays reachable — fall back so eBay still returns results.
        if not results and domain != "www.ebay.com":
            results = self._scrape_domain("www.ebay.com", query, prefer_local=False)
        return results

    def _scrape_domain(
        self, domain: str, query: SearchQuery, *, prefer_local: bool
    ) -> list[RawListing]:
        base = f"https://{domain}"
        params: dict[str, str] = {
            "_nkw": query.query,
            "_sop": _SOP.get(query.sort, "10"),
            "_ipg": "120",      # items per page
        }
        if prefer_local:
            params["LH_PrefLoc"] = "1"
        if query.price_min:
            params["_udlo"] = str(int(query.price_min))
        if query.price_max:
            params["_udhi"] = str(int(query.price_max))
        if query.condition and query.condition in _CONDITION:
            params["LH_ItemCondition"] = _CONDITION[query.condition]
        qs = "&".join(f"{k}={quote_plus(str(v))}" for k, v in params.items())
        url = f"{base}/sch/i.html?{qs}"

        html = self._fetch_via_curl(url, base)
        results = _parse_search_html(html, _domain_tz(domain))
        if not results and _looks_challenged(html):
            logger.warning("eBay %s returned a challenge page (bot check / IP).", domain)
        return results

    def _fetch_via_curl(self, url: str, base: str) -> str:
        """Fetch an eBay search page with a browser-impersonating TLS client.

        eBay serves a bot-check page to plain HTTP clients on their TLS
        fingerprint; curl_cffi impersonates a real browser, so a homepage warm-up
        + the search GET returns the real server-rendered HTML — no browser. A
        trusted session per domain is reused across searches; on a challenge the
        identity is rotated, and after exhausting retries that domain cools down.
        """
        if not HAVE_CURL_CFFI:
            raise RuntimeError(
                "eBay scrape mode needs curl_cffi (pip install curl_cffi), "
                "or set eBay API keys."
            )

        with _ebay_lock:
            st = _domain_state(base)
            # Skip fast if this domain recently hard-blocked us (stop hammering).
            if time.monotonic() < st["cooldown_until"]:
                logger.debug("eBay %s in cooldown — skipping this search.", base)
                return ""

            html = ""
            for _ in range(_MAX_FETCH_ATTEMPTS):
                # Reuse the trusted session if we have one; otherwise build a fresh
                # identity (new TLS profile) and warm it up on the homepage.
                if st["session"] is None:
                    st["session"] = cffi_requests.Session(
                        impersonate=random.choice(_IMPERSONATIONS)
                    )
                    st["session"].headers.update(_BROWSE_HEADERS)
                    try:
                        st["session"].get(f"{base}/", timeout=30.0)
                        time.sleep(random.uniform(0.4, 1.0))  # human-like pause
                    except Exception as exc:  # noqa: BLE001
                        logger.debug("eBay warm-up failed: %s", exc)
                        _close_session(st)
                        continue
                    # A bot-check solved in your browser + exported to cookies.txt
                    # lets the crawler reuse that clearance on this domain.
                    _inject_cookies(st["session"], self._settings.cookies_file, base)
                try:
                    html = st["session"].get(
                        url, timeout=30.0, headers={"Referer": f"{base}/"}
                    ).text
                except Exception as exc:  # noqa: BLE001
                    logger.debug("eBay fetch failed: %s", exc)
                    _close_session(st)
                    continue

                if not _looks_challenged(html):
                    st["cooldown_until"] = 0.0  # trusted again
                    return html  # keep the session — it's trusted

                # Challenged: drop this identity and rotate before retrying.
                _close_session(st)
                time.sleep(random.uniform(0.5, 1.2))

            # Every attempt was challenged — back off before trying this domain.
            st["cooldown_until"] = time.monotonic() + _COOLDOWN_SECONDS
            return html  # caller logs the warning


def _looks_challenged(html: str) -> bool:
    head = html[:1500].lower()
    return (
        "pardon our interruption" in head
        or "nous sommes" in head
        or "splashui/challenge" in head
    )


def _parse_search_html(html: str, tz=None) -> list[RawListing]:
    """Parse eBay's ``s-card`` search results into listings.

    Cards are sliced between consecutive ``<li class="s-card ...">`` openings
    (the markup nests no inner ``<li>``), and deduplicated by listing id — eBay
    renders hidden clipped duplicates of each card.
    """
    results: list[RawListing] = []
    seen: set[str] = set()
    starts = [m.start() for m in _CARD_OPEN_RE.finditer(html)]
    for idx, start in enumerate(starts):
        # Bound each card by the next card's start; the last one runs to the end
        # of the document. A fixed window truncated long cards (e.g. auction
        # listings with big embedded tracking JSON) before their price/date.
        end = starts[idx + 1] if idx + 1 < len(starts) else len(html)
        card = html[start:end]

        id_m = _LISTING_ID_RE.search(html[start:start + 400])
        link_m = _LINK_RE.search(card)
        if not id_m or not link_m:
            continue
        listing_id = id_m.group(1)
        if listing_id in seen:
            continue

        title = _parse_card_title(card)
        if title.lower() in _SKIP_TITLES:
            continue
        seen.add(listing_id)

        price, currency = _parse_html_price(card)
        thumb = _parse_thumbnail(card)
        results.append(
            RawListing(
                source=Source.EBAY,
                source_id=listing_id,
                title=title,
                url=unescape(link_m.group(1)).split("?")[0],
                price=price,
                currency=currency,
                thumbnail=thumb,
                photos=[thumb] if thumb else [],
                posted_at=_parse_listing_date(card, tz),
            )
        )
    return results


def _parse_card_title(card: str) -> str:
    # Image alt text is the clean product title (the title span may hold a
    # "Nouvelle annonce"/"Sponsored" badge instead).
    alt = _ALT_RE.search(card)
    if alt:
        title = unescape(alt.group(1)).strip()
        if title:
            return title
    m = _TITLE_RE.search(card)
    if m:
        return _strip_tags(m.group(1)).strip()
    return ""


def _parse_html_price(item_html: str) -> tuple[float | None, str]:
    m = _PRICE_RE.search(item_html)
    if not m:
        return None, "EUR"
    text = _strip_tags(m.group(1)).strip()
    val_m = _PRICE_VALUE_RE.search(text)
    if not val_m:
        return None, "EUR"
    price_str = val_m.group(1).replace(",", ".")
    currency = "EUR"
    if "USD" in text or "$" in text:
        currency = "USD"
    elif "GBP" in text or "£" in text:
        currency = "GBP"
    return _to_float(price_str), currency


def _parse_thumbnail(card: str) -> str | None:
    # The real product image lives on i.ebayimg.com (src or data-defer-load).
    m = _EBAYIMG_RE.search(card)
    if not m:
        return None
    return m.group(0).replace("/s-l225.", "/s-l500.").replace("/s-l140.", "/s-l500.")


def _strip_tags(html: str) -> str:
    return unescape(_TAG_RE.sub("", html))


def _to_float(value) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def fetch_detail(item_id: str) -> dict:
    """The seller's item description (lazy modal preview).

    eBay serves the description in a separate iframe document
    (``itm.ebaydesc.com``) — the search card has none. No extra photos are
    available there, so only the description is returned. Best-effort.
    """
    if not HAVE_CURL_CFFI or not item_id:
        return {"description": "", "photos": []}
    try:
        sess = cffi_requests.Session(impersonate=random.choice(_IMPERSONATIONS))
        html = sess.get(
            f"https://itm.ebaydesc.com/itmdesc/{item_id}", timeout=20.0
        ).text
        sess.close()
    except Exception as exc:  # noqa: BLE001
        logger.debug("eBay description fetch failed for %s: %s", item_id, exc)
        return {"description": "", "photos": []}
    return {"description": _clean_description(html), "photos": []}


_SCRIPT_STYLE_RE = re.compile(r"(?is)<(script|style)\b.*?</\1>")


def _clean_description(html: str) -> str:
    text = _SCRIPT_STYLE_RE.sub(" ", html)
    text = _TAG_RE.sub(" ", text)
    text = unescape(text)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\s*\n\s*", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    text = re.sub(r"^eBay\s+", "", text)  # drop the leading logo/title boilerplate
    return text[:3000]
