"""Persist raw listings, deduplicating on (source, source_id)."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import get_settings
from .geo import haversine_km
from .models import Listing
from .sources.base import RawListing
from .taxonomy import classify


def upsert_listing(session: Session, raw: RawListing) -> Listing | None:
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
        # Refresh price (it can change) but keep user-set status.
        if raw.price is not None:
            existing.price = raw.price
        return None

    model_guess, part_guess = classify(raw.title, raw.description)
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
        model_guess=model_guess.value,
        part_guess=part_guess.value,
        posted_at=raw.posted_at,
    )
    session.add(listing)
    return listing
