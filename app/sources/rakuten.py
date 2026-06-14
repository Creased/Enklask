"""Rakuten France source adapter (unofficial, JSON-LD based).

Rakuten's *official* Product Search API excludes used / C2C ("flea market")
listings — precisely the cheap/for-parts deals we want — and is seller-credential
gated. So this adapter instead reads the public search page and parses the
standard **JSON-LD** product data (``<script type="application/ld+json">``) that
Rakuten embeds for SEO. That's a stable, standards-based contract rather than a
guessed private API. Best-effort and isolated like the other unofficial sources.
"""

from __future__ import annotations

import json
import logging
import re
from urllib.parse import quote

import httpx

from ..config import get_settings
from ..enums import Source
from .base import BaseSource, RawListing, SearchQuery

logger = logging.getLogger(__name__)

_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
_LD_JSON_RE = re.compile(
    r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)
_ID_RE = re.compile(r"(\d{5,})")


class RakutenSource(BaseSource):
    name = Source.RAKUTEN

    def __init__(self) -> None:
        self._settings = get_settings()
        self._base = self._settings.rakuten_base_url.rstrip("/")

    @property
    def enabled(self) -> bool:
        return bool(self._settings.enable_rakuten)

    def search(self, query: SearchQuery) -> list[RawListing]:
        url = f"{self._base}/search/{quote(query.query)}"
        resp = httpx.get(
            url,
            headers={
                "User-Agent": _USER_AGENT,
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "fr-FR,fr;q=0.9",
            },
            timeout=30.0,
            follow_redirects=True,
        )
        resp.raise_for_status()
        products = _extract_products_from_html(resp.text)
        raws = [self._to_raw(p) for p in products]
        raws = [r for r in raws if r is not None]
        if query.price_max is not None:
            raws = [r for r in raws if r.price is None or r.price <= query.price_max]
        return raws

    def _to_raw(self, product: dict) -> RawListing | None:
        name = product.get("name")
        if not name:
            return None
        url = _first_str(product.get("url")) or self._base
        price, currency = _parse_offer(product.get("offers"))
        image = _first_image(product.get("image"))

        match = _ID_RE.search(url)
        source_id = match.group(1) if match else url

        return RawListing(
            source=Source.RAKUTEN,
            source_id=str(source_id),
            title=name,
            description=product.get("description", "") or "",
            url=url,
            price=price,
            currency=currency or "EUR",
            thumbnail=image,
            photos=[image] if image else [],
        )


def _extract_products_from_html(html: str) -> list[dict]:
    """Collect JSON-LD Product objects from a search page."""
    products: list[dict] = []
    seen: set[str] = set()
    for block in _LD_JSON_RE.findall(html or ""):
        try:
            data = json.loads(block)
        except json.JSONDecodeError:
            continue
        for product in _find_products(data):
            name = product.get("name")
            if name and name not in seen:
                seen.add(name)
                products.append(product)
    return products


def _find_products(node) -> list[dict]:
    """Recursively pull objects whose @type is Product (handles ItemList)."""
    found: list[dict] = []
    if isinstance(node, dict):
        if _is_type(node.get("@type"), "Product"):
            found.append(node)
        # ItemList wraps entries in itemListElement -> {item: {...}} or direct.
        for value in node.values():
            found.extend(_find_products(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_find_products(item))
    return found


def _is_type(value, target: str) -> bool:
    if isinstance(value, str):
        return value.lower() == target.lower()
    if isinstance(value, list):
        return any(isinstance(v, str) and v.lower() == target.lower() for v in value)
    return False


def _parse_offer(offers) -> tuple[float | None, str | None]:
    if isinstance(offers, list):
        offers = offers[0] if offers else None
    if not isinstance(offers, dict):
        return None, None
    raw_price = offers.get("price") or offers.get("lowPrice")
    currency = offers.get("priceCurrency")
    try:
        price = float(str(raw_price).replace(",", ".")) if raw_price is not None else None
    except (TypeError, ValueError):
        price = None
    return price, currency


def _first_image(image):
    if isinstance(image, list):
        image = image[0] if image else None
    if isinstance(image, dict):
        image = image.get("url")
    return image if isinstance(image, str) else None


def _first_str(value):
    if isinstance(value, list):
        value = value[0] if value else None
    return value if isinstance(value, str) else None
