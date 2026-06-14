"""Facebook Marketplace source adapter (experimental).

Facebook has no API for Marketplace. This adapter drives a logged-in headless
browser (Playwright) to load a Marketplace search page and scrape the result
cards. It is the most fragile and most ToS-sensitive source, so it is **off by
default** and only runs when both ``ENABLE_FACEBOOK=true`` and a logged-in
Playwright storage-state file are provided.

Playwright is an optional dependency (``requirements-scrapers.txt``); it is
imported lazily inside ``search`` so the rest of the app works without it.
"""

from __future__ import annotations

import logging
import os
import re
from urllib.parse import quote

from ..config import get_settings
from ..enums import Source
from .base import BaseSource, RawListing, SearchQuery

logger = logging.getLogger(__name__)

_ITEM_ID_RE = re.compile(r"/marketplace/item/(\d+)")
_PRICE_RE = re.compile(r"(\d[\d\s.,]*)\s*€")


class FacebookSource(BaseSource):
    name = Source.FACEBOOK

    def __init__(self) -> None:
        self._settings = get_settings()

    @property
    def enabled(self) -> bool:
        s = self._settings
        return bool(
            s.enable_facebook
            and s.facebook_storage_state
            and os.path.exists(s.facebook_storage_state)
        )

    def search(self, query: SearchQuery) -> list[RawListing]:
        cards = self._collect_cards(query.query)
        return self._parse_cards(cards)

    # -- browser interaction (not unit-tested; needs a live session) ---------
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
            # Scroll to load a few rows of results.
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

    # -- pure parsing (unit-tested) ------------------------------------------
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


def _extract_price(lines: list[str]) -> float | None:
    for line in lines:
        m = _PRICE_RE.search(line)
        if m:
            digits = re.sub(r"[^\d]", "", m.group(1))
            if digits:
                return float(digits)
    return None
