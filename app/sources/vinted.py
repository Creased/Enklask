"""Vinted source adapter (unofficial internal API).

Vinted has no public API. Its website talks to an internal JSON endpoint
(``/api/v2/catalog/items``) that works anonymously once a session cookie has
been obtained by first loading the homepage. This is unofficial and may break
when Vinted changes things — it is isolated so a failure never affects other
sources.
"""

from __future__ import annotations

import logging

import httpx

from ..config import get_settings
from ..enums import Source
from .base import BaseSource, RawListing, SearchQuery

logger = logging.getLogger(__name__)

_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


class VintedSource(BaseSource):
    name = Source.VINTED

    def __init__(self) -> None:
        self._settings = get_settings()
        self._base = self._settings.vinted_base_url.rstrip("/")

    @property
    def enabled(self) -> bool:
        return bool(self._settings.enable_vinted)

    def _client(self) -> httpx.Client:
        """A client that has bootstrapped Vinted session cookies."""
        client = httpx.Client(
            base_url=self._base,
            headers={
                "User-Agent": _USER_AGENT,
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "fr-FR,fr;q=0.9",
            },
            timeout=30.0,
            follow_redirects=True,
        )
        # Hitting the homepage sets the anonymous session cookie used by the API.
        client.get("/")
        return client

    def search(self, query: SearchQuery) -> list[RawListing]:
        params: dict[str, str | int] = {
            "search_text": query.query,
            "per_page": 60,
            "order": "newest_first",
            "currency": "EUR",
        }
        if query.price_max:
            params["price_to"] = query.price_max

        with self._client() as client:
            resp = client.get("/api/v2/catalog/items", params=params)
            resp.raise_for_status()
            items = resp.json().get("items", []) or []
        return [self._to_raw(item) for item in items]

    def _to_raw(self, item: dict) -> RawListing:
        price, currency = _parse_price(item)
        photo = item.get("photo") or {}
        full = photo.get("full_size_url") or photo.get("url")
        thumb = photo.get("url") or full
        photos = [full] if full else []

        url = item.get("url") or ""
        if url and url.startswith("/"):
            url = f"{self._base}{url}"

        return RawListing(
            source=Source.VINTED,
            source_id=str(item.get("id", "")),
            title=item.get("title", ""),
            url=url,
            price=price,
            currency=currency,
            thumbnail=thumb,
            photos=photos,
            location_city=item.get("city"),
        )


def _parse_price(item: dict) -> tuple[float | None, str]:
    price = item.get("price")
    # Newer API: {"amount": "12.0", "currency_code": "EUR"}
    if isinstance(price, dict):
        amount = price.get("amount")
        currency = price.get("currency_code", "EUR")
    else:
        amount = price
        currency = item.get("currency", "EUR")
    try:
        return (float(amount) if amount is not None else None), currency
    except (TypeError, ValueError):
        return None, currency
