"""Common interface shared by every marketplace adapter."""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from datetime import datetime

from ..enums import Source


@dataclass
class RawListing:
    """Source-agnostic ad payload, before classification/persistence.

    Adapters fill in whatever they can; everything except ``source``,
    ``source_id``, ``title`` and ``url`` is optional.
    """

    source: Source
    source_id: str
    title: str
    url: str
    description: str = ""
    price: float | None = None
    currency: str = "EUR"
    thumbnail: str | None = None
    photos: list[str] = field(default_factory=list)
    location_city: str | None = None
    lat: float | None = None
    lon: float | None = None
    distance_km: float | None = None
    shipping_options: list[str] = field(default_factory=list)
    # eBay-style extras (left None by sources that don't expose them).
    shipping_cost: float | None = None   # delivery fee; 0.0 = free, None = unknown
    buying_format: str | None = None     # "auction" | "buy_it_now"
    posted_at: datetime | None = None


class SearchQuery:
    """Lightweight view over a SavedSearch passed to adapters.

    Filters here are translated by each adapter into that marketplace's own
    native query parameters, so the crawl itself is filtered (not the results
    after the fact). ``condition`` is a generic bucket each adapter maps to its
    own codes; ``sort`` is one of relevance/recent/oldest/price_asc/price_desc.
    """

    def __init__(
        self,
        query: str,
        price_min: float | None = None,
        price_max: float | None = None,
        max_distance_km: float | None = None,
        condition: str | None = None,
        sort: str = "recent",
    ) -> None:
        self.query = query
        self.price_min = price_min
        self.price_max = price_max
        self.max_distance_km = max_distance_km
        self.condition = condition
        self.sort = sort


class BaseSource(abc.ABC):
    """Interface every adapter implements.

    Adapters are independent; the scheduler wraps each call in try/except so a
    single broken source never affects the others.
    """

    #: Stable identifier, matches the Source enum value.
    name: Source

    @property
    @abc.abstractmethod
    def enabled(self) -> bool:
        """Whether this source is configured and turned on."""

    @abc.abstractmethod
    def search(self, query: SearchQuery) -> list[RawListing]:
        """Return raw listings matching ``query`` (newest first when possible)."""

    def enrich(self, raw: RawListing) -> None:
        """Optionally fill in extra fields (description, date, …) on ``raw`` in
        place, for a listing that's about to be stored for the first time.

        Called once per newly-seen listing — never for already-known ones — so an
        adapter can fetch detail too expensive to gather for every search result.
        Must be best-effort and never raise. Default: no-op.
        """
        return None
