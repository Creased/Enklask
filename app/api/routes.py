"""JSON API: query listings, change status, trigger a refresh."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_session
from ..enums import ListingStatus
from ..models import Listing
from ..notifier import Notifier
from ..poller import poll_once
from ..schemas import ListingOut, PollResultOut

router = APIRouter(prefix="/api", tags=["api"])


def _query_listings(
    session: Session,
    *,
    source: str | None,
    model: str | None,
    part: str | None,
    status: str | None,
    price_max: float | None,
    distance_max: float | None,
    shipping: str | None,
    limit: int,
    offset: int,
) -> list[Listing]:
    stmt = select(Listing)
    if source:
        stmt = stmt.where(Listing.source == source)
    if model:
        stmt = stmt.where(Listing.model_guess == model)
    if part:
        stmt = stmt.where(Listing.part_guess == part)
    if status:
        stmt = stmt.where(Listing.status == status)
    else:
        # Hidden ads are excluded unless explicitly requested.
        stmt = stmt.where(Listing.status != ListingStatus.HIDDEN.value)
    if price_max is not None:
        stmt = stmt.where(Listing.price <= price_max)
    if distance_max is not None:
        stmt = stmt.where(Listing.distance_km <= distance_max)

    stmt = stmt.order_by(Listing.first_seen.desc()).limit(limit).offset(offset)
    rows = list(session.scalars(stmt))
    if shipping:
        rows = [r for r in rows if shipping in (r.shipping_options or [])]
    return rows


@router.get("/listings", response_model=list[ListingOut])
def list_listings(
    source: str | None = None,
    model: str | None = None,
    part: str | None = None,
    status: str | None = None,
    price_max: float | None = None,
    distance_max: float | None = None,
    shipping: str | None = None,
    limit: int = Query(60, le=200),
    offset: int = 0,
    session: Session = Depends(get_session),
):
    return _query_listings(
        session,
        source=source,
        model=model,
        part=part,
        status=status,
        price_max=price_max,
        distance_max=distance_max,
        shipping=shipping,
        limit=limit,
        offset=offset,
    )


@router.post("/listings/{listing_id}/status", response_model=ListingOut)
def set_status(
    listing_id: int,
    value: str = Query(..., description="new|seen|liked|hidden"),
    session: Session = Depends(get_session),
):
    try:
        new_status = ListingStatus(value)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid status: {value}")
    listing = session.get(Listing, listing_id)
    if listing is None:
        raise HTTPException(status_code=404, detail="Listing not found")
    listing.status = new_status.value
    session.commit()
    session.refresh(listing)
    return listing


@router.post("/notify/test")
def notify_test():
    """Send a sample notification to verify the Apprise configuration."""
    notifier = Notifier.from_settings()
    if not notifier.enabled:
        return {"enabled": False, "sent": False}
    return {"enabled": True, "sent": notifier.send_test()}


@router.post("/refresh", response_model=PollResultOut)
def refresh_now():
    result = poll_once()
    return PollResultOut(
        new_count=result.new_count,
        seen_count=result.seen_count,
        sources_run=result.sources_run,
        errors=result.errors,
    )
