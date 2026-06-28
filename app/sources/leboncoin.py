"""Leboncoin source adapter (curl_cffi self-minting transport).

Leboncoin's API sits behind DataDome, which blocks plain HTTP clients on their
TLS/JA3 fingerprint *before* the cookie is even checked. ``curl_cffi``
impersonates a real browser's TLS + HTTP/2 fingerprint: a homepage warm-up makes
DataDome issue a ``datadome`` cookie to the client itself, then POSTing the
search to ``/finder/search`` returns clean JSON — no browser, no manual cookie.
On a 403 the identity (UA + TLS profile + cookie) is rotated and retried.

The only requirement is a reasonably trusted (French residential) egress IP. If
``curl_cffi`` is unavailable, it falls back to an httpx POST seeded with a
``datadome`` cookie from cookies.txt.
"""

from __future__ import annotations

import logging
import os
import random
from datetime import datetime, timezone

try:  # Leboncoin timestamps are Europe/Paris local; normalize them to UTC.
    from zoneinfo import ZoneInfo

    _PARIS_TZ = ZoneInfo("Europe/Paris")
except Exception:  # pragma: no cover - missing tzdata
    _PARIS_TZ = None

import httpx

from ..config import get_settings
from ..cookies import load_cookies
from ..enums import Source
from ..shipping import detect_shipping
from .base import BaseSource, RawListing, SearchQuery

try:  # curl_cffi gives us a browser TLS fingerprint — the thing DataDome checks.
    from curl_cffi import requests as cffi_requests

    HAVE_CURL_CFFI = True
except ImportError:  # pragma: no cover
    HAVE_CURL_CFFI = False

logger = logging.getLogger(__name__)

_SEARCH_URL = "https://api.leboncoin.fr/finder/search"
_WEB_BASE = "https://www.leboncoin.fr"

# Public web api_key — optional once the datadome cookie is valid, sent for
# parity with the app. Refresh first if the API ever starts 401-ing.
_API_KEY = "ba0c2dad52b3ec"

# Browser TLS profiles curl_cffi can impersonate; one is picked per session so
# retries vary the fingerprint.
_IMPERSONATIONS = ["safari_ios", "chrome_android", "safari", "firefox"]
_ANDROID_MODELS = ["Pixel 7", "Pixel 8", "SM-G991B", "SM-S911B", "SM-A546B"]
_MAX_RETRIES = 4

# Generic sort -> Leboncoin (sort_by, sort_order).
_SORT = {
    "relevance": ("relevance", "desc"),
    "recent": ("time", "desc"),
    "oldest": ("time", "asc"),
    "price_asc": ("price", "asc"),
    "price_desc": ("price", "desc"),
}

# Generic condition -> Leboncoin item_condition (1 new … 5 for parts).
_CONDITION = {
    "new": "1",
    "like_new": "2",
    "good": "3",
    "fair": "4",
    "for_parts": "5",
}


def _mobile_ua() -> str:
    """A Leboncoin-app User-Agent: LBC;<OS>;<ver>;<model>;phone;<id>;wifi;<app>."""
    if random.random() < 0.5:
        ver = random.choice(["18.3", "18.5", "18.6", "26.0", "26.1"])
        return f"LBC;iOS;{ver};iPhone;phone;{os.urandom(8).hex()};wifi;101.44.0"
    ver = random.choice(["12", "13", "14", "15"])
    model = random.choice(_ANDROID_MODELS)
    return f"LBC;Android;{ver};{model};phone;{os.urandom(8).hex()};wifi;100.85.2"


class DataDomeBlocked(RuntimeError):
    """Raised when the API answers with a DataDome challenge (HTTP 403)."""


class LeboncoinSource(BaseSource):
    name = Source.LEBONCOIN

    def __init__(self) -> None:
        self._settings = get_settings()

    @property
    def enabled(self) -> bool:
        return bool(self._settings.enable_leboncoin)

    # -- public API ----------------------------------------------------------
    def search(self, query: SearchQuery) -> list[RawListing]:
        data = self._post(self._build_body(query))
        ads = data.get("ads") or []
        return [self._to_raw(ad) for ad in ads]

    def _build_body(self, query: SearchQuery) -> dict:
        sort_by, sort_order = _SORT.get(query.sort, ("time", "desc"))
        filters: dict = {
            "enums": {"ad_type": ["offer"]},
            "keywords": {"text": query.query, "type": "all"},
            "location": {"shippable": False},
        }
        price_range: dict = {}
        if query.price_min:
            price_range["min"] = int(query.price_min)
        if query.price_max:
            price_range["max"] = int(query.price_max)
        if price_range:
            filters["ranges"] = {"price": price_range}
        if query.condition and query.condition in _CONDITION:
            filters["enums"]["item_condition"] = [_CONDITION[query.condition]]
        return {
            "filters": filters,
            "limit": 35,
            "sort_by": sort_by,
            "sort_order": sort_order,
            "owner_type": "all",
            "listing_source": "direct-search",
        }

    # -- transport -----------------------------------------------------------
    def _datadome_cookie(self) -> str | None:
        """An optional datadome cookie to seed from cookies.txt (not required)."""
        jar = load_cookies(self._settings.cookies_file, ".leboncoin.fr")
        return jar.get("datadome")

    def _post(self, body: dict) -> dict:
        if HAVE_CURL_CFFI:
            return self._post_curl(body)
        # Fallback path: httpx is TLS-blocked unless a valid cookie is supplied.
        dd = self._datadome_cookie()
        if not dd:
            raise DataDomeBlocked(
                "curl_cffi is not installed and no datadome cookie is available. "
                "Install curl_cffi (the default engine) or export a datadome "
                "cookie into cookies.txt."
            )
        return self._post_httpx(body, dd)

    def _post_curl(self, body: dict) -> dict:
        seed = self._datadome_cookie()  # try a supplied cookie first, if any
        last = 0
        for attempt in range(_MAX_RETRIES + 1):
            session = self._new_curl_session(seed if attempt == 0 else None)
            try:
                resp = session.post(
                    _SEARCH_URL, json=body,
                    headers={"api_key": _API_KEY}, timeout=30.0,
                )
                last = resp.status_code
                if resp.status_code == 200:
                    return resp.json()
                if resp.status_code != 403:
                    raise DataDomeBlocked(f"HTTP {resp.status_code}")
            finally:
                try:
                    session.close()
                except Exception:  # noqa: BLE001
                    pass
            if attempt < _MAX_RETRIES:
                logger.info(
                    "Leboncoin 403 from DataDome; rotating identity (try %d/%d)",
                    attempt + 1, _MAX_RETRIES,
                )
        raise DataDomeBlocked(
            f"HTTP {last} after {_MAX_RETRIES} retries — the egress IP is likely "
            "not trusted by DataDome (use a French residential/mobile line)."
        )

    def _new_curl_session(self, datadome: str | None):
        session = cffi_requests.Session(impersonate=random.choice(_IMPERSONATIONS))
        session.headers.update({
            "User-Agent": _mobile_ua(),
            "Accept": "application/json",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-site",
        })
        if datadome:
            session.cookies.set("datadome", datadome, domain=".leboncoin.fr")
        else:
            # Warm up so DataDome issues a datadome cookie to this TLS client.
            try:
                session.get(f"{_WEB_BASE}/", timeout=30.0)
            except Exception as exc:  # noqa: BLE001 - warmup failure -> API 403 -> retry
                logger.debug("Leboncoin warm-up failed: %s", exc)
        return session

    def _post_httpx(self, body: dict, datadome: str) -> dict:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": _mobile_ua(),
            "api_key": _API_KEY,
            "Cookie": f"datadome={datadome}",
        }
        resp = httpx.post(_SEARCH_URL, json=body, headers=headers, timeout=30.0)
        if resp.status_code == 403:
            raise DataDomeBlocked(
                "HTTP 403 (httpx fallback) — cookie stale or IP untrusted."
            )
        resp.raise_for_status()
        return resp.json()

    # -- parsing -------------------------------------------------------------
    def _to_raw(self, ad: dict) -> RawListing:
        images = ad.get("images") or {}
        urls = images.get("urls") or []
        thumb = images.get("thumb_url") or images.get("small_url") or (urls[0] if urls else None)

        location = ad.get("location") or {}
        body = ad.get("body", "") or ""
        subject = ad.get("subject", "") or ""
        url = ad.get("url", "") or ""
        if url and url.startswith("/"):
            url = f"{_WEB_BASE}{url}"

        return RawListing(
            source=Source.LEBONCOIN,
            source_id=str(ad.get("list_id", "")),
            title=subject,
            description=body,
            url=url,
            price=_first_price(ad.get("price")),
            currency="EUR",
            thumbnail=thumb,
            photos=list(urls),
            location_city=location.get("city"),
            lat=location.get("lat"),
            lon=location.get("lng"),
            shipping_options=detect_shipping(subject, body),
            posted_at=_parse_date(ad.get("first_publication_date")),
        )


def _first_price(price) -> float | None:
    if isinstance(price, list):
        price = price[0] if price else None
    try:
        return float(price) if price is not None else None
    except (TypeError, ValueError):
        return None


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            dt = datetime.strptime(value, fmt)
        except ValueError:
            continue
        if dt.tzinfo is None and _PARIS_TZ is not None:
            dt = dt.replace(tzinfo=_PARIS_TZ)
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt
    return None
