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
    shipping_options: list[str] = field(default_factory=list)
    posted_at: datetime | None = None


class SearchQuery:
    """Lightweight view over a SavedSearch passed to adapters."""

    def __init__(
        self,
        query: str,
        price_max: float | None = None,
        max_distance_km: float | None = None,
    ) -> None:
        self.query = query
        self.price_max = price_max
        self.max_distance_km = max_distance_km


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
