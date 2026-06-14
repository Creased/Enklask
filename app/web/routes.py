"""HTML dashboard routes (server-rendered Jinja2 + htmx)."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from ..api.routes import _query_listings
from ..db import get_session
from ..enums import ConsoleModel, ListingStatus, PartType, Source
from ..models import Listing
from ..poller import poll_once
from ..sources.registry import get_enabled_sources

router = APIRouter(tags=["web"])

_TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

# Human-readable labels for the filter dropdowns.
SHIPPING_LABELS = {
    "vinted_go": "Vinted Go",
    "mondial_relay": "Mondial Relay",
    "pickup_point": "Point relais",
    "hand_delivery": "Main propre",
    "colissimo": "Colissimo",
    "chronopost": "Chronopost",
}


def _filter_context() -> dict:
    return {
        "sources": [s.value for s in Source],
        "models": [m.value for m in ConsoleModel],
        "parts": [p.value for p in PartType],
        "statuses": [s.value for s in ListingStatus],
        "shipping_labels": SHIPPING_LABELS,
    }


def _parse_filters(params) -> dict:
    def _clean(name: str) -> str | None:
        value = params.get(name)
        return value or None

    def _float(name: str) -> float | None:
        value = params.get(name)
        try:
            return float(value) if value else None
        except ValueError:
            return None

    return {
        "source": _clean("source"),
        "model": _clean("model"),
        "part": _clean("part"),
        "status": _clean("status"),
        "shipping": _clean("shipping"),
        "price_max": _float("price_max"),
        "distance_max": _float("distance_max"),
    }


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, session: Session = Depends(get_session)):
    filters = _parse_filters(request.query_params)
    listings = _query_listings(session, limit=60, offset=0, **filters)
    context = {
        "request": request,
        "listings": listings,
        "selected": filters,
        "enabled_sources": [s.name.value for s in get_enabled_sources()],
        **_filter_context(),
    }
    return templates.TemplateResponse("index.html", context)


@router.get("/partials/listings", response_class=HTMLResponse)
def listings_partial(request: Request, session: Session = Depends(get_session)):
    filters = _parse_filters(request.query_params)
    listings = _query_listings(session, limit=60, offset=0, **filters)
    return templates.TemplateResponse(
        "_grid.html", {"request": request, "listings": listings}
    )


@router.post("/partials/listings/{listing_id}/status", response_class=HTMLResponse)
def update_status(
    listing_id: int,
    request: Request,
    value: str = Form(...),
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

    # Hidden cards are removed from view; others are re-rendered in place.
    if new_status is ListingStatus.HIDDEN:
        return HTMLResponse("")
    return templates.TemplateResponse(
        "_card.html", {"request": request, "listing": listing}
    )


@router.post("/refresh", response_class=HTMLResponse)
def refresh(request: Request, session: Session = Depends(get_session)):
    poll_once()
    listings = _query_listings(session, limit=60, offset=0, **_parse_filters({}))
    return templates.TemplateResponse(
        "_grid.html", {"request": request, "listings": listings}
    )
