"""HTML dashboard routes (server-rendered Jinja2 + htmx)."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from ..api.routes import _query_listings
from ..config import get_settings, save_settings
from ..db import get_session
from ..enums import ListingStatus, Source
from ..models import Listing, ListingTopic, SavedSearch, Topic, _slugify
from ..poller import poll_once, poll_search, poll_topic
from ..scheduler import reschedule_poll
from ..sources.registry import get_enabled_sources

logger = logging.getLogger(__name__)

router = APIRouter(tags=["web"])

_TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

# Cache-bust static assets by their mtime so CSS/JS changes take effect without
# a manual hard-refresh (the value changes whenever the file is rebuilt).
try:
    _css_v = str(int(os.path.getmtime(Path(__file__).parent / "static" / "app.css")))
except OSError:
    _css_v = "1"
templates.env.globals["css_v"] = _css_v


def _timeago(value) -> str:
    """Format a datetime as a short French 'time ago' string (empty if None)."""
    if value is None:
        return ""
    from datetime import datetime, timezone

    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    secs = max((datetime.now(timezone.utc) - value).total_seconds(), 0)
    if secs < 90:
        return "à l'instant"
    mins = secs / 60
    if mins < 60:
        return f"il y a {int(mins)} min"
    hours = mins / 60
    if hours < 24:
        return f"il y a {int(hours)} h"
    days = hours / 24
    if days < 31:
        return f"il y a {int(days)} j"
    months = days / 30
    if months < 12:
        return f"il y a {int(months)} mois"
    return f"il y a {int(days / 365)} an{'s' if days >= 730 else ''}"


templates.env.filters["timeago"] = _timeago

SHIPPING_LABELS = {
    "vinted_go": "Vinted Go",
    "mondial_relay": "Mondial Relay",
    "pickup_point": "Point relais",
    "hand_delivery": "Main propre",
    "colissimo": "Colissimo",
    "chronopost": "Chronopost",
}

# Generic condition buckets each source maps to its own native codes.
CONDITION_LABELS = {
    "new": "Neuf",
    "like_new": "Comme neuf",
    "good": "Bon état",
    "fair": "Satisfaisant",
    "for_parts": "Pour pièces",
}

SORT_LABELS = {
    "relevance": "Pertinence",
    "recent": "Plus récentes",
    "oldest": "Plus anciennes",
    "price_asc": "Prix croissants",
    "price_desc": "Prix décroissants",
}

# Sort options for the liked price-watch page (computed in Python — the delta
# lives in the price_history JSON and can't be ordered in SQL).
LIKED_SORT_LABELS = {
    "price_drop": "Plus grosse baisse",
    "recent_move": "Mouvement récent",
    "price_rise": "Plus grosse hausse",
    "price_desc": "Prix décroissants",
}


def _filter_context() -> dict:
    return {
        "sources": [s.value for s in Source],
        "statuses": [s.value for s in ListingStatus],
        "shipping_labels": SHIPPING_LABELS,
        "condition_labels": CONDITION_LABELS,
        "sort_labels": SORT_LABELS,
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
        "status": _clean("status"),
        "shipping": _clean("shipping"),
        "keyword": _clean("keyword"),
        "price_min": _float("price_min"),
        "price_max": _float("price_max"),
        "distance_max": _float("distance_max"),
        "sort": _clean("sort"),
    }


_PAGE_SIZE = 60


def _listings_page(
    session: Session,
    *,
    topic_id: int | None,
    active_tags: list[str],
    filters: dict,
    offset: int = 0,
) -> dict:
    """Fetch one page of listings plus the htmx query string for the next page.

    Fetches one extra row to tell whether a next page exists, then trims it off.
    Returns the context shared by ``_grid.html`` and ``_grid_items.html``.
    """
    rows = _query_listings(
        session,
        topic_id=topic_id,
        tags=active_tags or None,
        limit=_PAGE_SIZE + 1,
        offset=offset,
        **filters,
    )
    has_more = len(rows) > _PAGE_SIZE
    rows = rows[:_PAGE_SIZE]

    params: list[tuple[str, str]] = []
    if topic_id is not None:
        params.append(("topic_id", str(topic_id)))
    for tag in active_tags:
        params.append(("tag", tag))
    for key, value in filters.items():
        if value not in (None, ""):
            params.append((key, str(value)))
    params.append(("offset", str(offset + _PAGE_SIZE)))

    return {
        "listings": rows,
        "has_more": has_more,
        "next_query": urlencode(params),
    }


# ---------------------------------------------------------------------------
# Home
# ---------------------------------------------------------------------------


def _topic_stat(session: Session, topic: Topic) -> dict:
    """Card stats for a topic: total listings, new count, sample thumbnails."""
    count = session.scalar(
        select(func.count())
        .select_from(ListingTopic)
        .where(ListingTopic.topic_id == topic.id)
    )
    new_count = session.scalar(
        select(func.count())
        .select_from(Listing)
        .join(ListingTopic, ListingTopic.listing_id == Listing.id)
        .where(
            ListingTopic.topic_id == topic.id,
            Listing.status == ListingStatus.NEW.value,
        )
    )
    thumbnails = list(
        session.scalars(
            select(Listing.thumbnail)
            .join(ListingTopic, ListingTopic.listing_id == Listing.id)
            .where(ListingTopic.topic_id == topic.id, Listing.thumbnail.isnot(None))
            .order_by(Listing.first_seen.desc())
            .limit(4)
        )
    )
    return {
        "topic": topic,
        "listing_count": count,
        "new_count": new_count,
        "search_count": len(topic.searches),
        "thumbnails": thumbnails,
    }


def _source_statuses(session: Session) -> list[dict]:
    """Per-source health for the home page.

    ok (green) = returning listings recently, warn (orange) = has listings but
    none lately, error (red) = enabled but nothing yet. Derived from
    ``last_seen`` (bumped on every poll for any source that returns results), so
    it needs no extra tracking and survives restarts.
    """
    from datetime import datetime, timedelta, timezone

    settings = get_settings()
    window = max(settings.poll_interval_minutes * 2, 20)
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=window)
    statuses: list[dict] = []
    for source in get_enabled_sources():
        name = source.name.value
        total = session.scalar(
            select(func.count()).select_from(Listing).where(Listing.source == name)
        ) or 0
        recent = session.scalar(
            select(func.count())
            .select_from(Listing)
            .where(Listing.source == name, Listing.last_seen >= cutoff)
        ) or 0
        state = "ok" if recent else ("warn" if total else "error")
        statuses.append({"name": name, "state": state, "recent": recent, "total": total})
    return statuses


@router.get("/", response_class=HTMLResponse)
def home(request: Request, session: Session = Depends(get_session)):
    topics = list(session.scalars(select(Topic).order_by(Topic.position, Topic.id)))
    return templates.TemplateResponse("home.html", {
        "request": request,
        "topic_stats": [_topic_stat(session, t) for t in topics],
        "source_status": _source_statuses(session),
    })


# ---------------------------------------------------------------------------
# Topic CRUD
# ---------------------------------------------------------------------------


@router.post("/topics", response_class=HTMLResponse)
async def create_topic(request: Request, session: Session = Depends(get_session)):
    form = await request.form()
    name = form.get("name", "").strip()
    if not name:
        return RedirectResponse("/", status_code=303)
    slug = _slugify(name)
    existing = session.scalar(select(Topic).where(Topic.slug == slug))
    if existing:
        return RedirectResponse(f"/t/{existing.slug}", status_code=303)
    topic = Topic(
        name=name,
        slug=slug,
        apprise_urls=form.get("apprise_urls", ""),
    )
    session.add(topic)
    session.commit()
    return RedirectResponse(f"/t/{topic.slug}", status_code=303)


# ---------------------------------------------------------------------------
# Topic detail
# ---------------------------------------------------------------------------


@router.get("/t/{slug}", response_class=HTMLResponse)
def topic_detail(
    slug: str, request: Request, session: Session = Depends(get_session)
):
    topic = session.scalar(select(Topic).where(Topic.slug == slug))
    if topic is None:
        raise HTTPException(status_code=404, detail="Topic not found")

    # Opening a topic clears its "nouvelles" badge: new listings become seen.
    session.execute(
        update(Listing)
        .where(
            Listing.status == ListingStatus.NEW.value,
            Listing.id.in_(
                select(ListingTopic.listing_id).where(
                    ListingTopic.topic_id == topic.id
                )
            ),
        )
        .values(status=ListingStatus.SEEN.value)
    )
    session.commit()

    filters = _parse_filters(request.query_params)
    active_tags = request.query_params.getlist("tag")

    page = _listings_page(
        session, topic_id=topic.id, active_tags=active_tags, filters=filters
    )

    listing_count = session.scalar(
        select(func.count())
        .select_from(ListingTopic)
        .where(ListingTopic.topic_id == topic.id)
    )

    all_topics = list(session.scalars(select(Topic).order_by(Topic.position, Topic.id)))

    return templates.TemplateResponse("topic.html", {
        "request": request,
        "topic": topic,
        "topics": all_topics,
        "selected": filters,
        "active_tags": active_tags,
        "listing_count": listing_count,
        "enabled_sources": [s.name.value for s in get_enabled_sources()],
        **page,
        **_filter_context(),
    })


# ---------------------------------------------------------------------------
# Topic settings
# ---------------------------------------------------------------------------


@router.get("/t/{slug}/settings", response_class=HTMLResponse)
def topic_settings(slug: str, request: Request, session: Session = Depends(get_session)):
    topic = session.scalar(select(Topic).where(Topic.slug == slug))
    if topic is None:
        raise HTTPException(status_code=404, detail="Topic not found")
    search_counts = {
        s.id: session.scalar(
            select(func.count())
            .select_from(ListingTopic)
            .where(ListingTopic.search_id == s.id)
        )
        for s in topic.searches
    }
    return templates.TemplateResponse("topic_settings.html", {
        "request": request,
        "topic": topic,
        "search_counts": search_counts,
        "condition_labels": CONDITION_LABELS,
    })


@router.post("/t/{slug}/settings", response_class=HTMLResponse)
async def save_topic_settings(
    slug: str, request: Request, session: Session = Depends(get_session)
):
    topic = session.scalar(select(Topic).where(Topic.slug == slug))
    if topic is None:
        raise HTTPException(status_code=404, detail="Topic not found")
    form = await request.form()
    topic.name = form.get("name", topic.name).strip()
    topic.slug = _slugify(topic.name)
    topic.apprise_urls = form.get("apprise_urls", topic.apprise_urls)
    session.commit()
    return RedirectResponse(f"/t/{topic.slug}/settings", status_code=303)


@router.post("/t/{slug}/delete")
def delete_topic(slug: str, session: Session = Depends(get_session)):
    topic = session.scalar(select(Topic).where(Topic.slug == slug))
    if topic is None:
        raise HTTPException(status_code=404, detail="Topic not found")
    session.delete(topic)
    session.commit()
    return RedirectResponse("/", status_code=303)


# ---------------------------------------------------------------------------
# Searches within a topic
# ---------------------------------------------------------------------------


@router.post("/t/{slug}/searches", response_class=HTMLResponse)
async def add_search(slug: str, request: Request, session: Session = Depends(get_session)):
    topic = session.scalar(select(Topic).where(Topic.slug == slug))
    if topic is None:
        raise HTTPException(status_code=404, detail="Topic not found")
    form = await request.form()
    query = form.get("query", "").strip()
    if not query:
        return RedirectResponse(f"/t/{slug}/settings", status_code=303)

    tags_raw = form.get("tags", "")
    tags = [t.strip() for t in tags_raw.split(",") if t.strip()]

    price_max = None
    pm = form.get("price_max", "")
    if pm:
        try:
            price_max = float(pm)
        except ValueError:
            pass

    search = SavedSearch(
        topic_id=topic.id,
        name=form.get("name", query[:64]),
        query=query,
        price_max=price_max,
        condition=(form.get("condition") or None),
        tags=tags,
    )
    session.add(search)
    session.commit()
    return RedirectResponse(f"/t/{slug}/settings", status_code=303)


@router.post("/t/{slug}/searches/{search_id}/delete")
def remove_search(slug: str, search_id: int, session: Session = Depends(get_session)):
    search = session.get(SavedSearch, search_id)
    if search:
        session.delete(search)
        session.commit()
    return RedirectResponse(f"/t/{slug}/settings", status_code=303)


@router.post("/t/{slug}/searches/{search_id}/toggle")
def toggle_search(slug: str, search_id: int, session: Session = Depends(get_session)):
    search = session.get(SavedSearch, search_id)
    if search:
        search.enabled = not search.enabled
        session.commit()
    return RedirectResponse(f"/t/{slug}/settings", status_code=303)


# ---------------------------------------------------------------------------
# Live search
# ---------------------------------------------------------------------------


@router.get("/search", response_class=HTMLResponse)
def search_page(request: Request, session: Session = Depends(get_session)):
    q = request.query_params.get("q", "")
    topics = list(session.scalars(select(Topic).order_by(Topic.position, Topic.id)))
    return templates.TemplateResponse("search.html", {
        "request": request,
        "q": q,
        "topics": topics,
        "enabled_sources": [s.name.value for s in get_enabled_sources()],
        **_filter_context(),
    })


def _interleave(lists: list[list]) -> list:
    """Round-robin merge so the first results show a mix of every source."""
    from itertools import chain, zip_longest

    return [x for x in chain.from_iterable(zip_longest(*lists)) if x is not None]


@router.get("/search/results", response_class=HTMLResponse)
def search_results_partial(request: Request):
    from ..geo import haversine_km
    from ..sources.base import SearchQuery

    q = request.query_params.get("q", "").strip()
    if not q:
        return templates.TemplateResponse("_search_results.html", {
            "request": request, "results": [], "q": "", "has_distance": False,
        })

    def _fnum(name: str) -> float | None:
        v = request.query_params.get(name, "")
        try:
            return float(v) if v else None
        except ValueError:
            return None

    sort = request.query_params.get("sort") or "recent"
    condition = request.query_params.get("condition") or None
    source_filter = request.query_params.get("source") or None

    # Filters are pushed into each source's native query (crawled, not
    # post-filtered) so the source returns only matching ads.
    query = SearchQuery(
        query=q,
        price_min=_fnum("price_min"),
        price_max=_fnum("price_max"),
        condition=condition,
        sort=sort,
    )

    sources = get_enabled_sources()
    if source_filter:
        sources = [s for s in sources if s.name.value == source_filter]

    per_source: list[list] = []
    for source in sources:
        try:
            found = source.search(query)
            if found:
                per_source.append(found)
        except Exception as exc:
            logger.warning("Live search: source %s failed: %s", source.name.value, exc)

    results = _interleave(per_source)

    # Each source already applied the sort; for price we still merge-sort across
    # sources (interleaving alternates them). Missing prices always sort last.
    if sort == "price_asc":
        results.sort(key=lambda r: (r.price is None, r.price or 0.0))
    elif sort == "price_desc":
        results.sort(key=lambda r: (r.price is None, -(r.price or 0.0)))

    # Distance whenever the ad carries coordinates (0.0 is a valid coordinate).
    settings = get_settings()
    for r in results:
        if r.lat is not None and r.lon is not None:
            r.distance_km = round(
                haversine_km(settings.home_lat, settings.home_lon, r.lat, r.lon), 1
            )

    has_distance = any(r.distance_km is not None for r in results)

    return templates.TemplateResponse("_search_results.html", {
        "request": request,
        "results": results,
        "q": q,
        "has_distance": has_distance,
    })


@router.post("/search/save", response_class=HTMLResponse)
async def save_search(request: Request, session: Session = Depends(get_session)):
    form = await request.form()
    query = form.get("query", "").strip()
    if not query:
        return RedirectResponse("/search", status_code=303)

    topic_id_str = form.get("topic_id", "")
    new_topic_name = form.get("new_topic_name", "").strip()
    tags = [t.strip() for t in form.get("tags", "").split(",") if t.strip()]

    # "new" (or any non-numeric value) means create a topic; fall back to the
    # query for its name if the field was left blank (e.g. first-ever topic).
    if topic_id_str == "new" or not topic_id_str.isdigit():
        name = new_topic_name or query[:64]
        slug = _slugify(name)
        topic = session.scalar(select(Topic).where(Topic.slug == slug))
        if not topic:
            topic = Topic(
                name=name,
                slug=slug,
            )
            session.add(topic)
            session.flush()
    else:
        topic = session.get(Topic, int(topic_id_str))

    if topic is None:
        return RedirectResponse("/search", status_code=303)

    search = SavedSearch(
        topic_id=topic.id,
        name=form.get("name") or query[:64],
        query=query,
        condition=(form.get("condition") or None),
        tags=tags,
    )
    session.add(search)
    session.commit()
    return RedirectResponse(f"/t/{topic.slug}", status_code=303)


# ---------------------------------------------------------------------------
# Listings partials (htmx)
# ---------------------------------------------------------------------------


@router.get("/partials/listings", response_class=HTMLResponse)
def listings_partial(request: Request, session: Session = Depends(get_session)):
    filters = _parse_filters(request.query_params)
    topic_id = request.query_params.get("topic_id")
    active_tags = request.query_params.getlist("tag")
    offset = int(request.query_params.get("offset") or 0)

    page = _listings_page(
        session,
        topic_id=int(topic_id) if topic_id else None,
        active_tags=active_tags,
        filters=filters,
        offset=offset,
    )
    # First page (offset 0) replaces the whole grid; later pages append items.
    template = "_grid_items.html" if offset else "_grid.html"
    return templates.TemplateResponse(template, {"request": request, **page})


@router.post("/partials/listings/{listing_id}/status", response_class=HTMLResponse)
def update_status(
    listing_id: int,
    request: Request,
    value: str = Form(...),
    from_: str = Form("", alias="from"),
    session: Session = Depends(get_session),
):
    try:
        new_status = ListingStatus(value)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid status: {value}")
    listing = session.get(Listing, listing_id)
    if listing is None:
        raise HTTPException(status_code=404, detail="Listing not found")

    # Toggle: clicking the active status again clears it (e.g. like -> unlike).
    if listing.status == new_status.value:
        listing.status = ListingStatus.NEW.value
    else:
        listing.status = new_status.value
    session.commit()
    session.refresh(listing)

    # On the price-watch page, unfollowing removes the row (empty swap); a still
    # -liked listing re-renders its row (safety — a toggle won't hit this).
    if from_ == "liked":
        if listing.status != ListingStatus.LIKED.value:
            # Unfollowed: remove the row + refresh the (out-of-band) summary so
            # the header count / tiles don't go stale.
            _, summary, _ = _watch_rows(session, None)
            return templates.TemplateResponse(
                "_watch_unfollow.html", {"request": request, "summary": summary}
            )
        return templates.TemplateResponse(
            "_watch_row.html", {"request": request, "row": _watch_row(listing)}
        )

    if listing.status == ListingStatus.HIDDEN.value:
        return HTMLResponse("")
    return templates.TemplateResponse(
        "_card.html", {"request": request, "listing": listing}
    )


# ---------------------------------------------------------------------------
# Refresh
# ---------------------------------------------------------------------------


@router.post("/refresh", response_class=HTMLResponse)
def refresh(request: Request, session: Session = Depends(get_session)):
    poll_once()
    page = _listings_page(
        session, topic_id=None, active_tags=[], filters=_parse_filters({})
    )
    return templates.TemplateResponse("_grid.html", {"request": request, **page})


@router.post("/refresh/{slug}", response_class=HTMLResponse)
def refresh_single(slug: str, request: Request, session: Session = Depends(get_session)):
    topic = session.scalar(select(Topic).where(Topic.slug == slug))
    if topic is None:
        raise HTTPException(status_code=404, detail="Topic not found")
    poll_topic(topic.id)
    session.expire_all()  # see listings the poller committed on other sessions

    # From the home topic list -> re-render just the topic card with fresh stats.
    if request.query_params.get("view") == "card":
        return templates.TemplateResponse(
            "_topic_card.html", {"request": request, "ts": _topic_stat(session, topic)}
        )

    filters = _parse_filters(request.query_params)
    active_tags = request.query_params.getlist("tag")
    page = _listings_page(
        session, topic_id=topic.id, active_tags=active_tags, filters=filters
    )
    return templates.TemplateResponse("_grid.html", {"request": request, **page})


@router.post("/t/{slug}/searches/{search_id}/refresh", response_class=HTMLResponse)
def refresh_search(
    slug: str, search_id: int, request: Request,
    session: Session = Depends(get_session),
):
    topic = session.scalar(select(Topic).where(Topic.slug == slug))
    if topic is None:
        raise HTTPException(status_code=404, detail="Topic not found")
    poll_search(search_id)
    session.expire_all()
    search = session.get(SavedSearch, search_id)
    if search is None:
        return HTMLResponse("")
    count = session.scalar(
        select(func.count())
        .select_from(ListingTopic)
        .where(ListingTopic.search_id == search_id)
    )
    return templates.TemplateResponse(
        "_search_row.html",
        {
            "request": request, "search": search, "topic": topic,
            "count": count, "condition_labels": CONDITION_LABELS,
        },
    )


# ---------------------------------------------------------------------------
# Liked items — price watch
# ---------------------------------------------------------------------------


def _sparkline_points(pts: list[float]) -> tuple[str, float, float]:
    """Build an SVG polyline (in a 120x32 box) from price points; higher price
    sits higher. Returns (points string, last x, last y). Caller guarantees ≥2 pts."""
    W, H, pad = 120, 32, 3
    lo, hi = min(pts), max(pts)
    span = (hi - lo) or 1.0
    n = len(pts)
    coords, x, y = [], 0.0, 0.0
    for i, p in enumerate(pts):
        x = pad + i * (W - 2 * pad) / (n - 1)
        y = (H - pad) - (p - lo) / span * (H - 2 * pad)
        coords.append(f"{x:.1f},{y:.1f}")
    return " ".join(coords), x, y


def _watch_row(listing: Listing) -> dict:
    """A price-tracking view over a liked listing: current/first price, delta
    since first seen, min/max, and a precomputed sparkline."""
    history = listing.price_history or []
    pts = [
        e["price"] for e in history
        if isinstance(e, dict) and e.get("price") is not None
    ]
    cur = listing.price if listing.price is not None else (pts[-1] if pts else None)
    first = pts[0] if pts else cur
    delta = (cur - first) if (cur is not None and first is not None) else 0.0
    # None (not 0%) when the base price is 0/None — avoids a misleading "+N € · +0 %".
    pct = (100.0 * delta / first) if first else None
    lo, hi = (min(pts), max(pts)) if pts else (cur, cur)
    moved = len(pts) >= 2
    points = last_x = last_y = None
    if moved:
        points, last_x, last_y = _sparkline_points(pts)
    direction = "down" if delta < 0 else ("up" if delta > 0 else "flat")
    return {
        "listing": listing, "cur": cur, "first": first, "delta": delta, "pct": pct,
        "lo": lo, "hi": hi, "moved": moved, "direction": direction,
        "points": points, "last_x": last_x, "last_y": last_y,
    }


def _last_move_at(listing: Listing) -> str:
    """ISO timestamp of the most recent price_history entry (sorts chronologically
    as a string since all entries are UTC isoformat)."""
    history = listing.price_history or []
    return history[-1].get("at", "") if history and isinstance(history[-1], dict) else ""


def _watch_rows(session: Session, sort: str | None) -> tuple[list[dict], dict, str]:
    listings = _query_listings(
        session, status=ListingStatus.LIKED.value, limit=500, offset=0
    )
    rows = [_watch_row(li) for li in listings]
    sort = sort if sort in LIKED_SORT_LABELS else "price_drop"
    if sort == "price_drop":
        rows.sort(key=lambda r: (r["delta"] >= 0, r["delta"]))
    elif sort == "price_rise":
        rows.sort(key=lambda r: r["delta"], reverse=True)
    elif sort == "recent_move":
        rows.sort(key=lambda r: _last_move_at(r["listing"]), reverse=True)
    elif sort == "price_desc":
        rows.sort(key=lambda r: (r["cur"] is None, -(r["cur"] or 0.0)))
    summary = {
        "count": len(rows),
        "dropped": sum(1 for r in rows if r["delta"] < 0),
        "savings": sum(-r["delta"] for r in rows if r["delta"] < 0),
        "any_moved": any(r["moved"] for r in rows),
    }
    return rows, summary, sort


@router.get("/liked", response_class=HTMLResponse)
def liked_page(request: Request, session: Session = Depends(get_session)):
    rows, summary, sort = _watch_rows(session, request.query_params.get("sort"))
    return templates.TemplateResponse("liked.html", {
        "request": request, "rows": rows, "summary": summary,
        "selected_sort": sort, "sort_labels": LIKED_SORT_LABELS,
    })


@router.get("/partials/liked", response_class=HTMLResponse)
def liked_partial(request: Request, session: Session = Depends(get_session)):
    rows, summary, _ = _watch_rows(session, request.query_params.get("sort"))
    return templates.TemplateResponse(
        "_watch_items.html", {"request": request, "rows": rows, "summary": summary}
    )


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    settings = get_settings()
    return templates.TemplateResponse(
        "settings.html", {"request": request, "settings": settings}
    )


@router.post("/settings", response_class=HTMLResponse)
async def settings_save(request: Request):
    form = await request.form()
    updates = {}

    bool_fields = [
        "enable_ebay", "enable_vinted", "enable_leboncoin",
        "enable_facebook", "enable_rakuten", "enable_geev",
        "notify_on_first_run",
    ]
    int_fields = ["poll_interval_minutes", "geev_radius_km", "notify_max_per_poll"]
    float_fields = ["home_lat", "home_lon"]
    str_fields = [
        "ebay_client_id", "ebay_client_secret", "ebay_marketplace_id",
        "vinted_base_url", "leboncoin_base_url",
        "facebook_storage_state", "facebook_city",
        "rakuten_base_url",
        "geev_base_url", "geev_web_base", "geev_api_key",
        "cookies_file",
        "apprise_urls",
    ]

    for f in bool_fields:
        updates[f] = f in form
    for f in int_fields:
        val = form.get(f, "")
        if val:
            updates[f] = int(val)
    for f in float_fields:
        val = form.get(f, "")
        if val:
            updates[f] = float(val)
    for f in str_fields:
        val = form.get(f, "")
        updates[f] = val

    old = get_settings()
    old_interval = old.poll_interval_minutes
    save_settings(updates)
    new = get_settings()
    if new.poll_interval_minutes != old_interval:
        reschedule_poll(new.poll_interval_minutes)

    return templates.TemplateResponse(
        "settings.html",
        {"request": request, "settings": new, "saved": True},
    )
