"""Vinted source adapter (unofficial internal API).

Vinted has no public API. Its website talks to an internal JSON endpoint
(``/api/v2/catalog/items``) that works anonymously once a session cookie has
been obtained by first loading the homepage. This is unofficial and may break
when Vinted changes things — it is isolated so a failure never affects other
sources.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone

import httpx

from ..config import get_settings
from ..enums import Source
from .base import BaseSource, RawListing, SearchQuery

logger = logging.getLogger(__name__)

_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# Generic sort -> Vinted "order" (no native oldest; falls back to newest).
_ORDER = {
    "relevance": "relevance",
    "recent": "newest_first",
    "oldest": "newest_first",
    "price_asc": "price_low_to_high",
    "price_desc": "price_high_to_low",
}

# Generic condition -> Vinted status id. 6 new w/ tag, 1 new w/o tag, 2 very good,
# 3 good, 4 satisfactory, 7 "certaines pièces ne fonctionnent pas" (for parts).
_CONDITION = {
    "new": "6",
    "like_new": "2",
    "good": "3",
    "fair": "4",
    "for_parts": "7",
}


class VintedSource(BaseSource):
    name = Source.VINTED

    def __init__(self) -> None:
        self._settings = get_settings()
        self._base = self._settings.vinted_base_url.rstrip("/")

    @property
    def enabled(self) -> bool:
        return bool(self._settings.enable_vinted)

    def _client(self) -> httpx.Client:
        """A fresh client configured for Vinted (no request issued yet)."""
        return httpx.Client(
            base_url=self._base,
            headers={
                "User-Agent": _USER_AGENT,
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "fr-FR,fr;q=0.9",
            },
            timeout=30.0,
            follow_redirects=True,
        )

    def search(self, query: SearchQuery) -> list[RawListing]:
        params: dict[str, str | int] = {
            "search_text": query.query,
            "per_page": 60,
            "order": _ORDER.get(query.sort, "newest_first"),
            "currency": "EUR",
        }
        if query.price_min:
            params["price_from"] = query.price_min
        if query.price_max:
            params["price_to"] = query.price_max
        if query.condition and query.condition in _CONDITION:
            params["status_ids[]"] = _CONDITION[query.condition]

        with self._client() as client:
            # Hitting the homepage sets the anonymous session cookie the API needs.
            client.get("/")
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
            posted_at=_photo_time(photo),
        )


def fetch_detail(item_id: str) -> dict:
    """Full description + photo gallery for one item (lazy modal preview).

    The catalog API only returns a cover photo and no description, and the item
    API (``/api/v2/items/{id}``) now 404s, so the item *page* is scraped: it
    embeds the item as JSON-string-escaped data. Best-effort — any failure
    returns empties so the preview still shows the card basics.
    """
    src = VintedSource()
    if not src.enabled or not item_id:
        return {"description": "", "photos": []}
    try:
        with src._client() as client:
            client.get("/")  # anonymous session cookie
            resp = client.get(
                f"/items/{item_id}",
                headers={"Accept": "text/html,application/xhtml+xml"},
            )
        html = resp.text if resp.status_code == 200 else ""
    except Exception as exc:  # noqa: BLE001
        logger.debug("Vinted detail fetch failed for %s: %s", item_id, exc)
        return {"description": "", "photos": []}
    return {
        "description": _extract_description(html),
        "photos": _extract_photos(html),
    }


def _extract_photos(html: str) -> list[str]:
    """Full-size URLs from the item's own (first) embedded ``photos`` array."""
    start = html.find('\\"photos\\":[')
    if start < 0:
        return []
    chunk = html[start:start + 60000].replace('\\"', '"')  # undo string-escaping
    bracket = chunk.find("[")
    depth = 0
    end = -1
    for k in range(bracket, len(chunk)):
        if chunk[k] == "[":
            depth += 1
        elif chunk[k] == "]":
            depth -= 1
            if depth == 0:
                end = k
                break
    arr = chunk[bracket:end + 1] if end > 0 else chunk[bracket:]
    out: list[str] = []
    seen: set[str] = set()
    for url in re.findall(r'"full_size_url":"(https://[^"]+)"', arr):
        if url not in seen:
            seen.add(url)
            out.append(url)
    return out


def _extract_description(html: str) -> str:
    """Decode the item's description from the double-escaped embedded JSON."""
    marker = '\\"description\\":'
    i = html.find(marker)
    if i < 0:
        return ""
    seg = html[i + len(marker):i + len(marker) + 12000]
    # The value is a JSON string that was itself JSON-string-encoded once more;
    # grab the doubly-escaped literal, then decode both levels.
    m = re.match(r'\s*(\\".*?\\")(?=,\\"|\})', seg, re.DOTALL)
    if not m:
        return ""
    try:
        inner = json.loads('"' + m.group(1) + '"')  # -> inner JSON literal
        return json.loads(inner).strip()            # -> real text
    except (ValueError, json.JSONDecodeError):
        return ""


def _photo_time(photo: dict) -> datetime | None:
    """Vinted's catalog API exposes no listing date; the main photo's upload
    timestamp is the closest proxy (photos are uploaded when an item is listed)."""
    ts = (photo.get("high_resolution") or {}).get("timestamp")
    try:
        if not ts:
            return None
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).replace(tzinfo=None)
    except (TypeError, ValueError, OSError):
        return None


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
