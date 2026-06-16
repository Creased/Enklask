"""Facebook Marketplace source adapter.

Facebook has no public Marketplace API, but the public search page works
**logged out**: it server-renders a Relay bootstrap that hands us an ``lsd``
token and the ``doc_id`` of the ``CometMarketplaceSearchContentContainerQuery``
GraphQL query, plus FB's own ``variables`` blob. We harvest those with a
browser-TLS fingerprint (``curl_cffi``, same transport as Leboncoin), replay the
GraphQL POST with our own home lat/lon + radius, and parse the listing feed — no
account, no browser.

The ``doc_id`` rotates with FB deploys and the ``lsd`` token is per-page, so both
are harvested fresh on every search rather than hardcoded.

If ``curl_cffi`` is unavailable or the anonymous path gets blocked, it falls back
to the legacy logged-in Playwright scraper — but only when a storage-state file
is configured. Playwright stays an optional dependency, imported lazily.
"""

from __future__ import annotations

import json
import logging
import os
import re
from urllib.parse import quote

from ..config import get_settings
from ..enums import Source
from .base import BaseSource, RawListing, SearchQuery

try:  # Browser-TLS fingerprint — lets the logged-out GraphQL POST through.
    from curl_cffi import requests as cffi_requests

    HAVE_CURL_CFFI = True
except ImportError:  # pragma: no cover
    HAVE_CURL_CFFI = False

logger = logging.getLogger(__name__)

_GRAPHQL_URL = "https://www.facebook.com/api/graphql/"
_FRIENDLY_NAME = "CometMarketplaceSearchContentContainerQuery"
_DEFAULT_RADIUS_KM = 80
_RESULT_COUNT = 24
_IMPERSONATE = "chrome"

# Bootstrap extraction patterns (logged-out search page).
_LSD_RE = re.compile(r'"LSD",\[\],\{"token":"([^"]+)"')
_PRELOADER_RE = re.compile(
    r'"adp_' + _FRIENDLY_NAME + r'RelayPreloader_[^"]*","queryID":"(\d+)","variables":'
)

_ITEM_ID_RE = re.compile(r"/marketplace/item/(\d+)")
_PRICE_RE = re.compile(r"(\d[\d\s.,]*)\s*€")

# Generic condition bucket -> FB Marketplace condition code. FB has no
# "for parts" equivalent, so it's left unfiltered.
_CONDITION = {
    "new": "new",
    "like_new": "used_like_new",
    "good": "used_good",
    "fair": "used_fair",
}


class FacebookBlocked(RuntimeError):
    """Raised when the anonymous path can't reach a usable Marketplace feed."""


class FacebookSource(BaseSource):
    name = Source.FACEBOOK

    def __init__(self) -> None:
        self._settings = get_settings()

    @property
    def enabled(self) -> bool:
        # The anonymous path needs no account, so the toggle alone enables it.
        return bool(self._settings.enable_facebook)

    def search(self, query: SearchQuery) -> list[RawListing]:
        if HAVE_CURL_CFFI:
            try:
                return self._search_anonymous(query)
            except FacebookBlocked as exc:
                if not self._has_login_session():
                    raise
                logger.warning(
                    "Facebook anonymous search blocked (%s); falling back to "
                    "logged-in browser.", exc,
                )
        if self._has_login_session():
            return self._parse_cards(self._collect_cards(query.query))
        raise FacebookBlocked(
            "curl_cffi is unavailable and no logged-in Playwright session is "
            "configured — cannot reach Facebook Marketplace."
        )

    def _has_login_session(self) -> bool:
        state = self._settings.facebook_storage_state
        return bool(state and os.path.exists(state))

    # -- anonymous GraphQL path (no account) ---------------------------------
    def _search_anonymous(self, query: SearchQuery) -> list[RawListing]:
        session = cffi_requests.Session(impersonate=_IMPERSONATE)
        session.headers.update({
            "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
        })
        try:
            lsd, doc_id, variables = self._harvest(session, query.query)
            self._apply_query(variables, query)
            payload = self._post_graphql(session, lsd, doc_id, variables)
        finally:
            try:
                session.close()
            except Exception:  # noqa: BLE001
                pass

        listings = self._parse_feed(payload)
        return _apply_price_filter(listings, query)

    def _harvest(self, session, text: str) -> tuple[str, str, dict]:
        """GET the search page and pull lsd, doc_id and FB's variables blob."""
        url = (
            "https://www.facebook.com/marketplace/search/"
            f"?query={quote(text)}&sortBy=creation_time_descend&exact=false"
        )
        resp = session.get(
            url,
            headers={"Accept": "text/html,application/xhtml+xml,*/*;q=0.8"},
            timeout=30.0,
            allow_redirects=True,
        )
        html = resp.text or ""
        if resp.status_code != 200:
            raise FacebookBlocked(f"search page HTTP {resp.status_code}")

        lsd_match = _LSD_RE.search(html)
        reg_match = _PRELOADER_RE.search(html)
        if not lsd_match or not reg_match:
            raise FacebookBlocked(
                "could not extract lsd/doc_id from the search page "
                "(layout changed or a login wall was served)."
            )
        variables = _extract_json_object(html, reg_match.end())
        if not isinstance(variables, dict):
            raise FacebookBlocked("could not extract GraphQL variables blob.")
        return lsd_match.group(1), reg_match.group(1), variables

    def _post_graphql(self, session, lsd: str, doc_id: str, variables: dict) -> dict:
        data = {
            "lsd": lsd,
            "fb_api_caller_class": "RelayModern",
            "fb_api_req_friendly_name": _FRIENDLY_NAME,
            "variables": json.dumps(variables),
            "doc_id": doc_id,
            "__comet_req": "15",
            "server_timestamps": "true",
        }
        resp = session.post(
            _GRAPHQL_URL,
            data=data,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "X-FB-LSD": lsd,
                "X-FB-Friendly-Name": _FRIENDLY_NAME,
                "Origin": "https://www.facebook.com",
                "Sec-Fetch-Site": "same-origin",
            },
            timeout=30.0,
        )
        if resp.status_code != 200:
            raise FacebookBlocked(f"GraphQL HTTP {resp.status_code}")
        body = resp.text or ""
        try:  # FB can stream multiple JSON lines; the first holds the feed.
            payload = json.loads(body.split("\n", 1)[0])
        except json.JSONDecodeError as exc:
            raise FacebookBlocked(f"GraphQL response not JSON: {exc}") from exc
        if payload.get("errors"):
            msg = payload["errors"][0].get("message", "unknown")
            raise FacebookBlocked(f"GraphQL error: {msg}")
        return payload

    def _apply_query(self, variables: dict, query: SearchQuery) -> None:
        """Force our query + home location + radius into FB's own variables."""
        s = self._settings
        radius = int(query.max_distance_km or _DEFAULT_RADIUS_KM)
        variables["count"] = _RESULT_COUNT
        variables["cursor"] = None
        variables["savedSearchQuery"] = query.query
        variables["shouldDeferNonCritical"] = False
        variables["shouldIncludePopularSearches"] = False
        variables["buyLocation"] = {"latitude": s.home_lat, "longitude": s.home_lon}

        params = variables.setdefault("params", {})
        params.setdefault("bqf", {})
        params["bqf"]["query"] = query.query
        params["bqf"]["callsite"] = "COMMERCE_MKTPLACE_WWW"

        brp = params.setdefault("browse_request_params", {})
        brp["filter_location_latitude"] = s.home_lat
        brp["filter_location_longitude"] = s.home_lon
        brp["filter_radius_km"] = radius
        if query.condition and query.condition in _CONDITION:
            brp["commerce_search_and_rp_condition"] = [_CONDITION[query.condition]]

    # -- pure feed parsing (unit-tested) -------------------------------------
    def _parse_feed(self, payload: dict) -> list[RawListing]:
        edges = (
            ((payload.get("data") or {}).get("marketplace_search") or {})
            .get("feed_units", {})
            .get("edges", [])
        ) or []
        listings: list[RawListing] = []
        seen: set[str] = set()
        for edge in edges:
            node = (edge or {}).get("node") or {}
            raw = self._listing_to_raw(node.get("listing") or {})
            if raw is None or raw.source_id in seen:
                continue
            seen.add(raw.source_id)
            listings.append(raw)
        return listings

    def _listing_to_raw(self, listing: dict) -> RawListing | None:
        item_id = str(listing.get("id") or "")
        if not item_id:
            return None
        # Skip listings that are no longer purchasable.
        if listing.get("is_sold") or listing.get("is_hidden") or listing.get("is_pending"):
            return None

        title = (
            listing.get("marketplace_listing_title")
            or listing.get("custom_title")
            or "Annonce Marketplace"
        )
        price_block = listing.get("listing_price") or {}
        price = _to_float(price_block.get("amount"))
        currency = _currency_from_formatted(price_block.get("formatted_amount"))

        photo = (
            ((listing.get("primary_listing_photo") or {}).get("image") or {}).get("uri")
        )
        geocode = (listing.get("location") or {}).get("reverse_geocode") or {}

        return RawListing(
            source=Source.FACEBOOK,
            source_id=item_id,
            title=title,
            url=f"https://www.facebook.com/marketplace/item/{item_id}/",
            price=price,
            currency=currency,
            thumbnail=photo,
            photos=[photo] if photo else [],
            location_city=geocode.get("city"),
        )

    # -- legacy logged-in Playwright fallback (not unit-tested) --------------
    def _collect_cards(self, text: str) -> list[dict]:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "Playwright is not installed. Install requirements-scrapers.txt "
                "and run `playwright install chromium`."
            ) from exc

        city = self._settings.facebook_city
        url = (
            f"https://www.facebook.com/marketplace/{city}/search/"
            f"?query={quote(text)}&sortBy=creation_time_descend"
        )

        results: list[dict] = []
        with sync_playwright() as p:  # pragma: no cover
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                storage_state=self._settings.facebook_storage_state
            )
            page = context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(4000)
            for _ in range(3):
                page.mouse.wheel(0, 4000)
                page.wait_for_timeout(1500)

            results = page.eval_on_selector_all(
                'a[href*="/marketplace/item/"]',
                """els => els.map(a => {
                    const img = a.querySelector('img');
                    return {
                        href: a.href,
                        text: a.innerText,
                        image: img ? img.src : null
                    };
                })""",
            )
            browser.close()
        return results

    def _parse_cards(self, cards: list[dict]) -> list[RawListing]:
        listings: list[RawListing] = []
        seen: set[str] = set()
        for card in cards:
            raw = self._parse_card(card)
            if raw is None or raw.source_id in seen:
                continue
            seen.add(raw.source_id)
            listings.append(raw)
        return listings

    def _parse_card(self, card: dict) -> RawListing | None:
        href = card.get("href") or ""
        match = _ITEM_ID_RE.search(href)
        if not match:
            return None
        item_id = match.group(1)

        lines = [ln.strip() for ln in (card.get("text") or "").splitlines() if ln.strip()]
        price = _extract_price(lines)
        non_price = [ln for ln in lines if "€" not in ln]
        title = non_price[0] if non_price else (lines[0] if lines else "Annonce Marketplace")
        location = non_price[-1] if len(non_price) > 1 else None

        return RawListing(
            source=Source.FACEBOOK,
            source_id=item_id,
            title=title,
            url=f"https://www.facebook.com/marketplace/item/{item_id}/",
            price=price,
            currency="EUR",
            thumbnail=card.get("image"),
            photos=[card["image"]] if card.get("image") else [],
            location_city=location,
        )


def _apply_price_filter(listings: list[RawListing], query: SearchQuery) -> list[RawListing]:
    """Filter by price using the reliable decimal amount (FB's native price
    bound unit is ambiguous, so we filter after parsing instead)."""
    lo, hi = query.price_min, query.price_max
    if lo is None and hi is None:
        return listings
    out = []
    for raw in listings:
        if raw.price is not None:
            if lo is not None and raw.price < lo:
                continue
            if hi is not None and raw.price > hi:
                continue
        out.append(raw)
    return out


def _currency_from_formatted(formatted: str | None) -> str:
    if not formatted:
        return "EUR"
    if "$" in formatted:
        return "USD"
    if "£" in formatted:
        return "GBP"
    return "EUR"


def _extract_price(lines: list[str]) -> float | None:
    for line in lines:
        m = _PRICE_RE.search(line)
        if m:
            digits = re.sub(r"[^\d]", "", m.group(1))
            if digits:
                return float(digits)
    return None


def _to_float(value, default=None):
    try:
        return float(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def _extract_json_object(text: str, start: int):
    """Brace-match the JSON object at the first ``{`` at/after *start*."""
    try:
        i = text.index("{", start)
    except ValueError:
        return None
    depth, in_str, esc = 0, False, False
    for j in range(i, len(text)):
        c = text[j]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[i : j + 1])
                except json.JSONDecodeError:
                    return None
    return None
