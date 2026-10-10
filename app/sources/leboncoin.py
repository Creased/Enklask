"""Leboncoin source adapter (curl_cffi browser transport).

Leboncoin's API sits behind DataDome, which blocks plain HTTP clients on their
TLS/JA3 fingerprint *before* the cookie is even checked. ``curl_cffi``
impersonates a real browser's TLS + HTTP/2 fingerprint. A passing session is
kept for later searches so its identity and refreshed ``datadome`` cookie age
together, matching the lifecycle used by DataDome's Android SDK. When the API
explicitly requests its JavaScript device check, the connector switches to a
persistent Camoufox browser context. An exported cookie remains available as an
httpx fallback when curl_cffi is unavailable.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from datetime import datetime, timezone
from typing import Any

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

# Public web-frontend api_key. It is not an account credential.
_API_KEY = "ba0c2dad52b3ec"

# Keep the TLS profile and curl_cffi-generated headers coherent. Desktop
# profiles paired with an app-style mobile User-Agent were measurably less
# reliable and create a fingerprint that no real client sends.
_MOBILE_IMPERSONATIONS = ("safari_ios", "chrome_android")
_MAX_FRESH_ATTEMPTS = 2
_BACKOFF_BASE_SECONDS = 2.0
_BLOCK_COOLDOWN_SECONDS = 120.0

# DataDome's Android SDK holds one cookie store for the app lifetime and
# serializes challenge handling. The source object itself is rebuilt for each
# poll, so the equivalent cache belongs at module scope.
_transport_lock = threading.RLock()
_cached_session: Any | None = None
_cached_profile: str | None = None
_blocked_until = 0.0
_browser_required = False

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


class DataDomeBlocked(RuntimeError):
    """Raised when the API answers with a DataDome challenge (HTTP 403)."""

    def __init__(
        self,
        message: str,
        *,
        challenge: bool = False,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.challenge = challenge
        self.retryable = retryable


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
        global _browser_required

        if _browser_required:
            return self._post_browser(body)
        if HAVE_CURL_CFFI:
            try:
                return self._post_curl(body)
            except DataDomeBlocked as exc:
                if not exc.challenge:
                    raise
                logger.info(
                    "Leboncoin requested a DataDome device check; switching to "
                    "the persistent browser transport"
                )
                _browser_required = True
                return self._post_browser(body)
        # Fallback path: httpx is TLS-blocked unless a valid cookie is supplied.
        dd = self._datadome_cookie()
        if not dd:
            return self._post_browser(body)
        return self._post_httpx(body, dd)

    def _post_browser(self, body: dict) -> dict:
        from .leboncoin_browser import fetch_leboncoin_json

        return fetch_leboncoin_json(
            _WEB_BASE,
            self._settings.leboncoin_browser_profile_dir,
            body,
            _API_KEY,
        )

    def _post_curl(self, body: dict) -> dict:
        global _blocked_until, _cached_profile, _cached_session

        with _transport_lock:
            remaining = _blocked_until - time.monotonic()
            if remaining > 0:
                raise DataDomeBlocked(
                    f"DataDome cooldown active ({remaining:.0f}s remaining)"
                )

            # First reuse the last identity that completed a search. The SDK in
            # the Android app follows the same model and replaces its cookie
            # whenever a response supplies a newer one.
            if _cached_session is not None:
                resp = self._search_request(_cached_session, body)
                if resp.status_code == 200:
                    return resp.json()
                if not _is_datadome_challenge(resp):
                    raise DataDomeBlocked(f"HTTP {resp.status_code}")
                logger.info(
                    "Leboncoin cached %s identity was challenged (X-DD-B=%s)",
                    _cached_profile,
                    _challenge_marker(resp),
                )
                _close_session(_cached_session)
                _cached_session = None
                _cached_profile = None
                raise DataDomeBlocked(
                    f"HTTP {resp.status_code} DataDome device check",
                    challenge=True,
                )

            seed = self._datadome_cookie()
            last_status = 0
            last_marker: str | None = None
            for attempt in range(_MAX_FRESH_ATTEMPTS):
                profile = random.choice(_MOBILE_IMPERSONATIONS)
                session = self._new_curl_session(seed if attempt == 0 else None, profile)
                try:
                    self._warm_up(session)
                    resp = self._search_request(session, body)
                    last_status = resp.status_code
                    last_marker = _challenge_marker(resp)
                    if resp.status_code == 200:
                        _cached_session = session
                        _cached_profile = profile
                        return resp.json()
                    if _is_datadome_challenge(resp):
                        raise DataDomeBlocked(
                            f"HTTP {resp.status_code} DataDome device check",
                            challenge=True,
                        )
                    raise DataDomeBlocked(f"HTTP {resp.status_code}")
                except DataDomeBlocked as exc:
                    if exc.challenge:
                        raise
                    if not exc.retryable:
                        raise
                    last_status = getattr(exc, "status_code", last_status)
                    logger.info("Leboncoin identity rejected during warm-up: %s", exc)
                finally:
                    if session is not _cached_session:
                        _close_session(session)

                if attempt + 1 < _MAX_FRESH_ATTEMPTS:
                    delay = random.uniform(
                        _BACKOFF_BASE_SECONDS * (2**attempt),
                        _BACKOFF_BASE_SECONDS * (2**attempt) * 1.75,
                    )
                    logger.info(
                        "Leboncoin DataDome challenge (X-DD-B=%s); retrying a "
                        "fresh mobile identity in %.1fs",
                        last_marker,
                        delay,
                    )
                    time.sleep(delay)

            _blocked_until = time.monotonic() + _BLOCK_COOLDOWN_SECONDS
            raise DataDomeBlocked(
                f"HTTP {last_status or 403} DataDome challenge after "
                f"{_MAX_FRESH_ATTEMPTS} paced attempts; pausing Leboncoin for "
                f"{int(_BLOCK_COOLDOWN_SECONDS)}s",
                challenge=bool(last_marker),
            )

    def _new_curl_session(self, datadome: str | None, profile: str):
        session = cffi_requests.Session(impersonate=profile)
        # Let curl_cffi generate the User-Agent and browser-controlled headers
        # that match its TLS profile. DataDome's Android interceptor itself only
        # forces Accept and merges its cookie into the existing cookie jar.
        session.headers.update({"Accept": "application/json"})
        if datadome:
            session.cookies.set("datadome", datadome, domain=".leboncoin.fr")
        return session

    def _warm_up(self, session) -> None:
        """Require a clean homepage response before spending an API request."""
        resp = session.get(f"{_WEB_BASE}/", timeout=30.0)
        if resp.status_code != 200:
            exc = DataDomeBlocked(
                f"warm-up HTTP {resp.status_code}", retryable=True
            )
            exc.status_code = resp.status_code
            raise exc
        if not _session_cookie(session, "datadome"):
            raise DataDomeBlocked(
                "warm-up returned 200 without a datadome cookie",
                retryable=True,
            )

    @staticmethod
    def _search_request(session, body: dict):
        return session.post(
            _SEARCH_URL,
            json=body,
            headers={"api_key": _API_KEY},
            timeout=30.0,
        )

    def _post_httpx(self, body: dict, datadome: str) -> dict:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
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


def _challenge_marker(response) -> str | None:
    return response.headers.get("X-DD-B") or response.headers.get("X-SF-CC-X-dd-b")


def _is_datadome_challenge(response) -> bool:
    """Mirror the Android SDK's response gate before invoking DD handling."""
    return response.status_code in (401, 403) and bool(_challenge_marker(response))


def _close_session(session) -> None:
    try:
        session.close()
    except Exception:  # noqa: BLE001 - best-effort transport cleanup
        pass


def _session_cookie(session, name: str) -> str | None:
    """Read a cookie without assuming a particular curl_cffi cookie backend."""
    try:
        value = session.cookies.get(name)
        return str(value) if value else None
    except (AttributeError, KeyError):
        pass
    try:
        for cookie in session.cookies.jar:
            if cookie.name == name:
                return str(cookie.value)
    except (AttributeError, TypeError):
        pass
    return None


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
