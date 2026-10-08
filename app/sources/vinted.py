"""Vinted source adapter (unofficial browser scraper).

Vinted has no public API. Search results are server-rendered into the Next.js
React Server Components payload of ``/catalog``. The old
``/api/v2/catalog/items`` endpoint is no longer used by the website.

The catalogue sits behind Cloudflare, so a persistent Camoufox session warms
the homepage before loading search and item pages.

This is unofficial and may break when Vinted changes things — it is isolated so
a failure never affects other sources.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone

from ..config import get_settings
from ..enums import Source
from .base import BaseSource, RawListing, SearchQuery
from .vinted_browser import fetch_vinted_html

logger = logging.getLogger(__name__)

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


class VintedParseError(RuntimeError):
    """Raised when Vinted's catalogue no longer contains the expected payload."""


class VintedSource(BaseSource):
    name = Source.VINTED

    def __init__(self) -> None:
        self._settings = get_settings()
        self._base = self._settings.vinted_base_url.rstrip("/")

    @property
    def enabled(self) -> bool:
        return bool(self._settings.enable_vinted)

    def search(self, query: SearchQuery) -> list[RawListing]:
        params: dict[str, str | int] = {
            "search_text": query.query,
            "order": _ORDER.get(query.sort, "newest_first"),
        }
        if query.price_min:
            params["price_from"] = query.price_min
        if query.price_max:
            params["price_to"] = query.price_max
        if query.condition and query.condition in _CONDITION:
            params["status_ids[]"] = _CONDITION[query.condition]

        html = self._get_html("/catalog", params=params)
        items = _extract_catalog_items(html)
        if items is None:
            raise VintedParseError(
                "Vinted returned a catalogue page without its Next.js items payload."
            )
        return [self._to_raw(item) for item in items]

    def _get_html(self, path: str, *, params: dict | None = None) -> str:
        return fetch_vinted_html(
            self._base,
            self._settings.vinted_browser_profile_dir,
            path,
            params,
        )

    def _to_raw(self, item: dict) -> RawListing:
        # Current catalogue SSR shape wraps each listing in ``productItem``;
        # retaining the old flat shape keeps saved fixtures/backward parsing.
        item = item.get("productItem") or item
        price, currency = _parse_price(item)
        photo = item.get("photo") or {}
        current_photos = item.get("photos") or []
        photos = [p.get("url") for p in current_photos if p.get("url")]
        full = photo.get("full_size_url") or photo.get("url")
        if not photos and full:
            photos = [full]
        thumb = item.get("thumbnailUrl") or photo.get("url") or full

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
        html = src._get_html(f"/items/{item_id}")
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
    arr = _balanced_json(chunk, bracket, "[", "]")
    try:
        photos = json.loads(arr)
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for photo in photos:
        if not isinstance(photo, dict):
            continue
        url = photo.get("full_size_url") or photo.get("url")
        if url and url not in seen:
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
        currency = price.get("currency_code") or price.get("currencyCode", "EUR")
    else:
        amount = price
        currency = item.get("currency", "EUR")
    try:
        return (float(amount) if amount is not None else None), currency
    except (TypeError, ValueError):
        return None, currency


_NEXT_PUSH = "self.__next_f.push("
_ITEMS_MARKER = '"items":{"items":['


def _extract_catalog_items(html: str) -> list[dict] | None:
    """Extract catalogue items from Vinted's Next.js RSC bootstrap payload.

    Each inline script calls ``self.__next_f.push([id, "..."])``. The second
    value is a decoded RSC stream containing ordinary JSON fragments. We locate
    the catalogue state and parse only its balanced ``items`` array instead of
    depending on unstable generated class names in the rendered HTML.
    """
    scripts = re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", html, re.I | re.S)
    for script in scripts:
        text = script.strip()
        if not text.startswith(_NEXT_PUSH) or not text.endswith(")"):
            continue
        try:
            pushed = json.loads(text[len(_NEXT_PUSH):-1])
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(pushed, list) or len(pushed) < 2:
            continue
        payload = pushed[1]
        if not isinstance(payload, str):
            continue

        offset = 0
        while True:
            marker = payload.find(_ITEMS_MARKER, offset)
            if marker < 0:
                break
            start = marker + len(_ITEMS_MARKER) - 1
            raw = _balanced_json(payload, start, "[", "]")
            if raw:
                try:
                    items = json.loads(raw)
                except (TypeError, ValueError, json.JSONDecodeError):
                    items = []
                if isinstance(items, list) and (
                    not items
                    or any(
                        isinstance(item, dict) and item.get("productItem")
                        for item in items
                    )
                ):
                    return items
            offset = start + 1
    return None


def _balanced_json(text: str, start: int, opener: str, closer: str) -> str:
    """Return one balanced JSON container while respecting quoted strings."""
    if start >= len(text) or text[start] != opener:
        return ""
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == opener:
            depth += 1
        elif char == closer:
            depth -= 1
            if depth == 0:
                return text[start:index + 1]
    return ""
