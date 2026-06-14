"""eBay source adapter using the official Browse API.

Uses client-credentials OAuth (an application token, no user login needed).
Docs: https://developer.ebay.com/api-docs/buy/browse/overview.html
"""

from __future__ import annotations

import base64
import logging
import time
from datetime import datetime

import httpx

from ..config import get_settings
from ..enums import Source
from .base import BaseSource, RawListing, SearchQuery

logger = logging.getLogger(__name__)

_OAUTH_URL = "https://api.ebay.com/identity/v1/oauth2/token"
_SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
_SCOPE = "https://api.ebay.com/oauth/api_scope"


class EbaySource(BaseSource):
    name = Source.EBAY

    def __init__(self) -> None:
        self._settings = get_settings()
        self._token: str | None = None
        self._token_expiry: float = 0.0

    @property
    def enabled(self) -> bool:
        s = self._settings
        return bool(s.enable_ebay and s.ebay_client_id and s.ebay_client_secret)

    # -- auth ----------------------------------------------------------------
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

    # -- search --------------------------------------------------------------
    def search(self, query: SearchQuery) -> list[RawListing]:
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
        return [self._to_raw(item) for item in items]

    def _to_raw(self, item: dict) -> RawListing:
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
