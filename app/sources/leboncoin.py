"""Leboncoin source adapter (unofficial internal API + browser fallback).

Leboncoin exposes no public API. Its site/app call ``/finder/search`` on
``api.leboncoin.fr``, which is protected by DataDome anti-bot. This adapter is
**best effort**:

1. It first tries the JSON API with browser-like headers.
2. If DataDome blocks that call and Playwright is installed, it falls back to a
   real headless browser that renders the search page and extracts ads from the
   embedded ``__NEXT_DATA__`` JSON.
3. If both fail, it raises a clear error; the poller records it and keeps the
   other sources running.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from urllib.parse import quote

import httpx

from ..config import get_settings
from ..enums import Source
from ..taxonomy import detect_shipping
from .base import BaseSource, RawListing, SearchQuery

logger = logging.getLogger(__name__)

# Public key the Leboncoin web frontend sends. It can change over time; if the
# API starts returning 401/403, this is the first thing to refresh.
_API_KEY = "ba0c2dad52b3ec"

_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

_WEB_BASE = "https://www.leboncoin.fr"
_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
    re.DOTALL,
)


class DataDomeBlocked(RuntimeError):
    """Raised when the JSON API is blocked by DataDome anti-bot."""


class LeboncoinSource(BaseSource):
    name = Source.LEBONCOIN

    def __init__(self) -> None:
        self._settings = get_settings()
        self._base = self._settings.leboncoin_base_url.rstrip("/")

    @property
    def enabled(self) -> bool:
        return bool(self._settings.enable_leboncoin)

    def search(self, query: SearchQuery) -> list[RawListing]:
        try:
            return self._search_api(query)
        except DataDomeBlocked as exc:
            logger.info("Leboncoin API blocked, trying browser fallback: %s", exc)
            return self._search_browser(query)

    # -- 1) JSON API ---------------------------------------------------------
    def _search_api(self, query: SearchQuery) -> list[RawListing]:
        payload: dict = {
            "filters": {
                "enums": {"ad_type": ["offer"]},
                "keywords": {"text": query.query},
            },
            "limit": 35,
            "sort_by": "time",
            "sort_order": "desc",
        }
        if query.price_max:
            payload["filters"]["ranges"] = {"price": {"max": int(query.price_max)}}

        headers = {
            "User-Agent": _USER_AGENT,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "api_key": _API_KEY,
            "Origin": _WEB_BASE,
            "Referer": f"{_WEB_BASE}/",
        }
        resp = httpx.post(
            f"{self._base}/finder/search",
            json=payload,
            headers=headers,
            timeout=30.0,
        )
        if resp.status_code in (401, 403) or "datadome" in resp.text.lower():
            raise DataDomeBlocked(f"HTTP {resp.status_code}")
        resp.raise_for_status()
        ads = resp.json().get("ads", []) or []
        return [self._to_raw(ad) for ad in ads]

    # -- 2) Playwright browser fallback --------------------------------------
    def _search_browser(self, query: SearchQuery) -> list[RawListing]:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "Leboncoin blocked by DataDome and Playwright is not installed. "
                "Install requirements-scrapers.txt and run "
                "`playwright install chromium` to enable the browser fallback."
            ) from exc

        url = f"{_WEB_BASE}/recherche?text={quote(query.query)}&sort=time"
        if query.price_max:
            url += f"&price=min-{int(query.price_max)}"

        html = ""
        with sync_playwright() as p:  # pragma: no cover - needs a browser
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent=_USER_AGENT, locale="fr-FR"
            )
            page = context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(3000)
            html = page.content()
            browser.close()

        ads = _extract_ads_from_html(html)
        if not ads:
            raise RuntimeError(
                "Leboncoin browser fallback returned no ads (page may still be "
                "challenged by DataDome, or the markup changed)."
            )
        return [self._to_raw(ad) for ad in ads]

    # -- shared parsing ------------------------------------------------------
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


def _extract_ads_from_html(html: str) -> list[dict]:
    """Pull ad dicts out of the page's embedded ``__NEXT_DATA__`` JSON."""
    match = _NEXT_DATA_RE.search(html or "")
    if not match:
        return []
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError:
        return []
    return _find_ads(data)


def _find_ads(node, seen: set | None = None) -> list[dict]:
    """Recursively collect objects that look like Leboncoin ads.

    Resilient to layout changes: any dict carrying both ``list_id`` and
    ``subject`` is treated as an ad, deduplicated by ``list_id``.
    """
    if seen is None:
        seen = set()
    found: list[dict] = []
    if isinstance(node, dict):
        if "list_id" in node and "subject" in node:
            key = str(node.get("list_id"))
            if key not in seen:
                seen.add(key)
                found.append(node)
        for value in node.values():
            found.extend(_find_ads(value, seen))
    elif isinstance(node, list):
        for item in node:
            found.extend(_find_ads(item, seen))
    return found


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
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None
