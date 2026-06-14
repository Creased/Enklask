"""Leboncoin source adapter (unofficial internal API).

Leboncoin exposes no public API. Its site/app call ``/finder/search`` on
``api.leboncoin.fr``, which is protected by DataDome anti-bot. This adapter is
**best effort**: it tries the JSON API with browser-like headers and, if it gets
blocked, raises a clear error (the poller records it and keeps other sources
running). A Playwright fallback can be added when the optional scraper deps are
installed.
"""

from __future__ import annotations

import logging
from datetime import datetime

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


class LeboncoinSource(BaseSource):
    name = Source.LEBONCOIN

    def __init__(self) -> None:
        self._settings = get_settings()
        self._base = self._settings.leboncoin_base_url.rstrip("/")

    @property
    def enabled(self) -> bool:
        return bool(self._settings.enable_leboncoin)

    def search(self, query: SearchQuery) -> list[RawListing]:
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
            "Origin": "https://www.leboncoin.fr",
            "Referer": "https://www.leboncoin.fr/",
        }
        resp = httpx.post(
            f"{self._base}/finder/search",
            json=payload,
            headers=headers,
            timeout=30.0,
        )
        if resp.status_code in (401, 403) or "datadome" in resp.text.lower():
            raise RuntimeError(
                "Leboncoin blocked the request (DataDome). A Playwright fallback "
                "or residential proxy is needed — see requirements-scrapers.txt."
            )
        resp.raise_for_status()
        ads = resp.json().get("ads", []) or []
        return [self._to_raw(ad) for ad in ads]

    def _to_raw(self, ad: dict) -> RawListing:
        images = ad.get("images") or {}
        urls = images.get("urls") or []
        thumb = images.get("thumb_url") or images.get("small_url") or (urls[0] if urls else None)

        location = ad.get("location") or {}
        body = ad.get("body", "") or ""
        subject = ad.get("subject", "") or ""

        return RawListing(
            source=Source.LEBONCOIN,
            source_id=str(ad.get("list_id", "")),
            title=subject,
            description=body,
            url=ad.get("url", ""),
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
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None
