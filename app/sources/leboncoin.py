"""Leboncoin source adapter (curl_cffi browser transport).

Leboncoin's API sits behind DataDome, which blocks plain HTTP clients on their
TLS/JA3 fingerprint *before* the cookie is even checked. ``curl_cffi``
impersonates a real browser's TLS + HTTP/2 fingerprint. A passing session is
kept for later searches so its identity and refreshed ``datadome`` cookie age
together, matching the lifecycle used by DataDome's Android SDK. ``curl_cffi``
can clear DataDome's TLS gate but cannot run its JavaScript device check, so
when the API (or the homepage warm-up) is challenged the connector hands that
search to a persistent Camoufox browser — which runs the JS and clears it —
and then prefers the browser for a short window instead of re-triggering curl
challenges it cannot solve. An exported cookie remains available as an httpx
fallback when curl_cffi is unavailable.

Set ``leboncoin_proxy`` (or ``LEBONCOIN_PROXY`` / ``LBC_PROXY``) to route all
Leboncoin traffic through a separate egress: scraping DataDome from a
residential IP you also browse from can taint that IP's bot score for every
device on it.
"""

from __future__ import annotations

import logging
import os
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
from .leboncoin_browser import LeboncoinBrowserError

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
# After curl hits a DataDome challenge, pause curl for this long: retrying a
# transport that cannot solve the JS check only deepens the IP's bot score.
_BLOCK_COOLDOWN_SECONDS = 120.0
# After the browser clears a challenge, prefer it for this long instead of
# re-triggering curl challenges — but fall back to curl afterwards so one
# transient block does not pin the whole process to the heavy browser.
_BROWSER_PREFER_SECONDS = 300.0

# DataDome's Android SDK holds one cookie store for the app lifetime and
# serializes challenge handling. The source object itself is rebuilt for each
# poll, so the equivalent cache belongs at module scope.
_transport_lock = threading.RLock()
_cached_session: Any | None = None
_cached_profile: str | None = None
_blocked_until = 0.0
_browser_preferred_until = 0.0

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
    """Raised when the API answers with a DataDome challenge or hard block.

    ``challenge`` marks a DataDome device check that only a real browser can
    clear, so the caller should switch to the Camoufox transport. ``retryable``
    marks a transient curl failure (warm-up transport error / ambiguous
    non-200) that is worth another fresh identity.
    """

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
        self._proxy = (
            getattr(self._settings, "leboncoin_proxy", None)
            or os.environ.get("LEBONCOIN_PROXY")
            or os.environ.get("LBC_PROXY")
            or None
        )

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
        global _browser_preferred_until

        with _transport_lock:
            prefer_browser = _browser_preferred_until > time.monotonic()

        if prefer_browser:
            try:
                return self._post_browser(body)
            except LeboncoinBrowserError as exc:
                logger.info("Leboncoin browser preference failed (%s); retrying curl", exc)
                with _transport_lock:
                    _browser_preferred_until = 0.0
                # fall through to curl

        if HAVE_CURL_CFFI:
            try:
                return self._post_curl(body)
            except DataDomeBlocked as exc:
                # Only a DataDome challenge is worth the heavyweight browser; a
                # hard non-challenge failure propagates so other sources run.
                if not exc.challenge or prefer_browser:
                    raise
                try:
                    data = self._post_browser(body)
                except LeboncoinBrowserError as bexc:
                    # Browser unavailable/failed: do NOT pin to it; surface the
                    # original block so the poller keeps the other sources.
                    raise exc from bexc
                with _transport_lock:
                    _browser_preferred_until = time.monotonic() + _BROWSER_PREFER_SECONDS
                return data

        # Fallback path: httpx is TLS-blocked unless a valid cookie is supplied.
        dd = self._datadome_cookie()
        if dd:
            return self._post_httpx(body, dd)
        return self._post_browser(body)

    def _post_browser(self, body: dict) -> dict:
        # Call module-qualified so tests can monkeypatch fetch_leboncoin_json.
        from . import leboncoin_browser

        return leboncoin_browser.fetch_leboncoin_json(
            _WEB_BASE,
            self._settings.leboncoin_browser_profile_dir,
            body,
            _API_KEY,
            proxy=self._proxy,
        )

    def _post_curl(self, body: dict) -> dict:
        global _blocked_until, _cached_profile, _cached_session

        with _transport_lock:
            remaining = _blocked_until - time.monotonic()
            if remaining > 0:
                # Curl is paused after a recent block; ask _post for the browser.
                raise DataDomeBlocked(
                    f"DataDome cooldown active ({remaining:.0f}s remaining)",
                    challenge=True,
                )

            # 1) Reuse the identity that last completed a search. DataDome's
            #    Android SDK follows the same model, replacing its cookie
            #    whenever a response supplies a newer one.
            if _cached_session is not None:
                try:
                    resp = self._search_request(_cached_session, body)
                except Exception as exc:  # noqa: BLE001 - transport error -> rotate
                    logger.info("Leboncoin cached session transport error: %s", exc)
                    _close_session(_cached_session)
                    _cached_session = _cached_profile = None
                else:
                    data = _json_or_none(resp)
                    if data is not None:
                        return data
                    # The cached identity no longer works: never keep a dead
                    # session, or every later poll repeats the same failure.
                    challenge = resp.status_code == 200 or _is_datadome_challenge(resp)
                    logger.info(
                        "Leboncoin cached %s identity rejected (HTTP %s); rotating",
                        _cached_profile,
                        resp.status_code,
                    )
                    _close_session(_cached_session)
                    _cached_session = _cached_profile = None
                    if challenge:
                        _blocked_until = time.monotonic() + _BLOCK_COOLDOWN_SECONDS
                        raise DataDomeBlocked(
                            f"HTTP {resp.status_code} DataDome device check (cached)",
                            challenge=True,
                        )
                    # otherwise fall through and rotate a fresh identity

            # 2) Fresh identities. A real DataDome challenge short-circuits to
            #    the browser; only transient/ambiguous failures are retried.
            seed = self._datadome_cookie()
            last_status = 0
            for attempt in range(_MAX_FRESH_ATTEMPTS):
                profile = random.choice(_MOBILE_IMPERSONATIONS)
                session = self._new_curl_session(seed if attempt == 0 else None, profile)
                try:
                    self._warm_up(session)
                    resp = self._search_request(session, body)
                except DataDomeBlocked as exc:
                    last_status = getattr(exc, "status_code", 0) or last_status
                    _close_session(session)
                    if exc.challenge:
                        _blocked_until = time.monotonic() + _BLOCK_COOLDOWN_SECONDS
                        raise DataDomeBlocked(
                            "DataDome challenged the warm-up; switching to the browser",
                            challenge=True,
                        )
                    # transient warm-up / transport issue -> try a fresh identity
                except Exception as exc:  # noqa: BLE001 - search transport error
                    logger.info("Leboncoin search transport error: %s", exc)
                    _close_session(session)
                else:
                    data = _json_or_none(resp)
                    if data is not None:
                        _cached_session = session
                        _cached_profile = profile
                        return data
                    last_status = resp.status_code
                    challenge = resp.status_code == 200 or _is_datadome_challenge(resp)
                    _close_session(session)
                    if challenge:
                        _blocked_until = time.monotonic() + _BLOCK_COOLDOWN_SECONDS
                        raise DataDomeBlocked(
                            f"HTTP {resp.status_code} DataDome device check",
                            challenge=True,
                        )
                    # ambiguous non-200 (429/5xx/markerless) -> rotate identity

                if attempt + 1 < _MAX_FRESH_ATTEMPTS:
                    delay = _backoff(attempt)
                    logger.info(
                        "Leboncoin curl attempt %d failed (HTTP %s); retrying in %.1fs",
                        attempt + 1, last_status or "?", delay,
                    )
                    time.sleep(delay)

            raise DataDomeBlocked(
                f"HTTP {last_status or 0} after {_MAX_FRESH_ATTEMPTS} curl attempts",
                challenge=False,
            )

    def _new_curl_session(self, datadome: str | None, profile: str):
        kwargs: dict[str, Any] = {"impersonate": profile}
        if self._proxy:
            kwargs["proxies"] = {"https": self._proxy, "http": self._proxy}
        session = cffi_requests.Session(**kwargs)
        # Let curl_cffi generate the User-Agent and browser-controlled headers
        # that match its TLS profile. DataDome's Android interceptor itself only
        # forces Accept and merges its cookie into the existing cookie jar.
        session.headers.update({"Accept": "application/json"})
        if datadome:
            session.cookies.set("datadome", datadome, domain=".leboncoin.fr")
        return session

    def _warm_up(self, session) -> None:
        """Require a clean homepage response before spending an API request."""
        try:
            resp = session.get(f"{_WEB_BASE}/", timeout=30.0)
        except Exception as exc:  # noqa: BLE001 - transient transport failure
            raise DataDomeBlocked(f"warm-up transport error: {exc}", retryable=True)
        if resp.status_code != 200:
            # A homepage non-200 is a DataDome verdict (nothing else blocks it).
            exc = DataDomeBlocked(
                f"warm-up HTTP {resp.status_code}",
                challenge=resp.status_code in (401, 403) or _is_datadome_challenge(resp),
                retryable=True,
            )
            exc.status_code = resp.status_code
            raise exc
        if not _session_cookie(session, "datadome"):
            # A 200 shell without a datadome cookie is a soft challenge page.
            raise DataDomeBlocked(
                "warm-up returned 200 without a datadome cookie",
                challenge=True,
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
        if self._proxy:
            with httpx.Client(proxy=self._proxy, timeout=30.0) as client:
                resp = client.post(_SEARCH_URL, json=body, headers=headers)
        else:
            resp = httpx.post(_SEARCH_URL, json=body, headers=headers, timeout=30.0)
        if resp.status_code == 403:
            raise DataDomeBlocked(
                "HTTP 403 (httpx fallback) — cookie stale or IP untrusted.",
                challenge=True,
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


def _backoff(attempt: int) -> float:
    base = _BACKOFF_BASE_SECONDS * (2 ** attempt)
    return random.uniform(base, base * 1.75)


_CHALLENGE_HEADERS = ("X-DD-B", "X-SF-CC-X-dd-b")
_CHALLENGE_BODY_HINTS = ("datadome", "captcha-delivery")


def _challenge_marker(response) -> str | None:
    for name in _CHALLENGE_HEADERS:
        value = response.headers.get(name)
        if value:
            return str(value)
    return None


def _is_datadome_challenge(response) -> bool:
    """Mirror the Android SDK's response gate before invoking DD handling.

    A DataDome device check is a 401/403 carrying the X-DD-B marker; the marker
    header name varies, so a captcha-delivery / datadome body is accepted too.
    """
    if response.status_code not in (401, 403):
        return False
    if _challenge_marker(response):
        return True
    try:
        body = (response.text or "")[:2000].lower()
    except Exception:  # noqa: BLE001 - mocks / binary bodies have no .text
        return False
    return any(hint in body for hint in _CHALLENGE_BODY_HINTS)


def _json_or_none(response):
    """Parsed JSON dict for a genuine 200, else None.

    A DataDome soft challenge can arrive as HTTP 200 with an HTML/JS body, so a
    200 that does not parse to a JSON object is treated as *not* a success —
    never cached, routed to the browser instead.
    """
    if response.status_code != 200:
        return None
    try:
        data = response.json()
    except Exception:  # noqa: BLE001 - 200 with HTML/challenge body
        return None
    return data if isinstance(data, dict) else None


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
