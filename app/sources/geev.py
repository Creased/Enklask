"""Geev source adapter (unofficial internal API).

Geev is a French give-away / cheap-classifieds app — a great source for free or
near-free **donor units** ("pour pièces", "ne s'allume plus") near you, and it
exposes coordinates so distance-from-home works well.

Geev has no public API, so this hits its internal endpoint. The exact request
shape isn't officially documented, so ``search`` is best-effort while the parsing
(``_extract_articles`` / ``_to_raw``) is defensive and unit-tested. Like the other
unofficial sources it is isolated: a failure is recorded and never affects others.
If it returns nothing, capture one real request from the app/DevTools and the
endpoint/params can be adjusted.
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


class GeevSource(BaseSource):
    name = Source.GEEV

    def __init__(self) -> None:
        self._settings = get_settings()
        self._base = self._settings.geev_base_url.rstrip("/")
        self._web = self._settings.geev_web_base.rstrip("/")

    @property
    def enabled(self) -> bool:
        return bool(self._settings.enable_geev)

    def search(self, query: SearchQuery) -> list[RawListing]:
        s = self._settings
        params = {
            "text": query.query,
            "latitude": s.home_lat,
            "longitude": s.home_lon,
            "distance": s.geev_radius_km,
            "mode": "object",
        }
        headers = {
            "User-Agent": _USER_AGENT,
            "Accept": "application/json",
        }
        if s.geev_api_key:
            headers["Authorization"] = f"Bearer {s.geev_api_key}"

        resp = httpx.get(
            f"{self._base}/v1/articles/search",
            params=params,
            headers=headers,
            timeout=30.0,
        )
        resp.raise_for_status()
        articles = _extract_articles(resp.json())
        return [self._to_raw(a) for a in articles]

    def _to_raw(self, article: dict) -> RawListing:
        article_id = str(article.get("id") or article.get("_id") or "")
        lat, lon = _coords(article)
        city = _city(article)
        photos = _photos(article)

        return RawListing(
            source=Source.GEEV,
            source_id=article_id,
            title=article.get("title", "") or "",
            description=article.get("description", "") or "",
            url=f"{self._web}/fr/ad/{article_id}",
            # Geev objects are donations (free); keep 0 so price filters include them.
            price=_to_float(article.get("price"), default=0.0),
            currency="EUR",
            thumbnail=photos[0] if photos else None,
            photos=photos,
            location_city=city,
            lat=lat,
            lon=lon,
        )


def _extract_articles(payload) -> list[dict]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("articles", "data", "results", "items"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
    return []


def _coords(article: dict) -> tuple[float | None, float | None]:
    lat = article.get("latitude")
    lon = article.get("longitude")
    if lat is None or lon is None:
        location = article.get("location") or {}
        lat = lat if lat is not None else location.get("latitude")
        lon = lon if lon is not None else location.get("longitude")
    return _to_float(lat), _to_float(lon)


def _city(article: dict) -> str | None:
    city = article.get("city")
    if city:
        return city
    location = article.get("location") or {}
    return location.get("city")


def _photos(article: dict) -> list[str]:
    raw = article.get("pictures") or article.get("images") or []
    urls: list[str] = []
    for item in raw:
        if isinstance(item, str):
            urls.append(item)
        elif isinstance(item, dict):
            url = item.get("url") or item.get("src")
            if url:
                urls.append(url)
    return urls


def _to_float(value, default=None):
    try:
        return float(value) if value is not None else default
    except (TypeError, ValueError):
        return default
