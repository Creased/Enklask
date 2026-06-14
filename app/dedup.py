"""Persist raw listings, deduplicating on (source, source_id)."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import get_settings
from .geo import haversine_km
from .models import Listing, ListingTopic
from .sources.base import RawListing


def _iso(dt: datetime | None) -> str:
    if dt is None:
        return datetime.now(timezone.utc).isoformat()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def upsert_listing(
    session: Session,
    raw: RawListing,
    *,
    topic_id: int | None = None,
    search_id: int | None = None,
    tags: list[str] | None = None,
) -> Listing | None:
    """Insert ``raw`` as a Listing, or refresh ``last_seen`` if already known.

    Returns the newly-created Listing, or ``None`` if it already existed.
    """
    existing = session.scalar(
        select(Listing).where(
            Listing.source == raw.source.value,
            Listing.source_id == raw.source_id,
        )
    )
    if existing is not None:
        existing.last_seen = datetime.now(timezone.utc)
        # Record a price change so drops/rises can be shown. Seed the prior price
        # the first time so the very first change keeps both endpoints.
        if raw.price is not None and raw.price != existing.price:
            history = list(existing.price_history or [])
            if not history and existing.price is not None:
                history.append({"price": existing.price, "at": _iso(existing.first_seen)})
            history.append({"price": raw.price, "at": _iso(None)})
            existing.price_history = history  # reassign so SQLAlchemy persists it
            existing.price = raw.price
        # Backfill a publish date we didn't have before (e.g. eBay listings
        # saved before date parsing existed).
        if existing.posted_at is None and raw.posted_at is not None:
            existing.posted_at = raw.posted_at
        # Link to topic if not already linked.
        if topic_id is not None:
            already_linked = session.scalar(
                select(ListingTopic).where(
                    ListingTopic.listing_id == existing.id,
                    ListingTopic.topic_id == topic_id,
                )
            )
            if not already_linked:
                link = ListingTopic(
                    listing_id=existing.id,
                    topic_id=topic_id,
                    search_id=search_id,
                    tags=tags or [],
                )
                session.add(link)
        return None

    settings = get_settings()
    distance = None
    if raw.lat is not None and raw.lon is not None:
        distance = round(
            haversine_km(settings.home_lat, settings.home_lon, raw.lat, raw.lon), 1
        )

    listing = Listing(
        source=raw.source.value,
        source_id=raw.source_id,
        title=raw.title,
        description=raw.description,
        url=raw.url,
        price=raw.price,
        currency=raw.currency,
        thumbnail=raw.thumbnail,
        photos=raw.photos,
        location_city=raw.location_city,
        distance_km=distance,
        shipping_options=raw.shipping_options,
        posted_at=raw.posted_at,
        price_history=(
            [{"price": raw.price, "at": _iso(None)}] if raw.price is not None else []
        ),
    )
    session.add(listing)
    session.flush()

    if topic_id is not None:
        link = ListingTopic(
            listing_id=listing.id,
            topic_id=topic_id,
            search_id=search_id,
            tags=tags or [],
        )
        session.add(link)

    return listing
