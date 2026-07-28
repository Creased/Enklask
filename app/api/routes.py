"""JSON API: topics, searches, listings, and system controls."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func, nullslast, select
from sqlalchemy.orm import Session

from ..config import get_settings, save_settings
from ..db import get_session
from ..enums import ListingStatus
from ..models import Listing, ListingTopic, SavedSearch, Topic, _slugify
from ..notifier import Notifier
from ..poller import poll_once, poll_topic
from ..scheduler import reschedule_poll
from ..schemas import ListingOut, PollResultOut, SavedSearchOut, TopicOut

router = APIRouter(prefix="/api", tags=["api"])

logger = logging.getLogger(__name__)

# Lazily-fetched listing detail (full description + photo gallery) for the
# preview modal, cached so reopening an item doesn't refetch from the source.
_PREVIEW_CACHE: dict[str, dict] = {}
_PREVIEW_FETCHERS: dict = {}


@router.get("/preview")
def listing_preview(source: str, id: str):
    """Full description + photos for one listing, fetched on demand (modal).

    Sources expose the cover photo and no description in search results; the
    detail lives behind a per-item fetch, so it's done lazily here (only when a
    user opens the preview) and cached, never during crawls.
    """
    key = f"{source}:{id}"
    cached = _PREVIEW_CACHE.get(key)
    if cached is not None:
        return cached

    if not _PREVIEW_FETCHERS:
        from ..sources import ebay, vinted
        _PREVIEW_FETCHERS.update(ebay=ebay.fetch_detail, vinted=vinted.fetch_detail)

    fetcher = _PREVIEW_FETCHERS.get(source)
    data = {"description": "", "photos": []}
    if fetcher:
        try:
            data = fetcher(id)
        except Exception:  # noqa: BLE001
            logger.warning("Preview fetch failed for %s", key, exc_info=True)

    if len(_PREVIEW_CACHE) > 500:
        _PREVIEW_CACHE.clear()
    _PREVIEW_CACHE[key] = data
    return data


# ---------------------------------------------------------------------------
# Listing queries
# ---------------------------------------------------------------------------


# Order by the listing's own publish date (what the card shows), falling back to
# when we first saw it for sources that don't expose a post date (e.g. eBay).
_DATE_KEY = func.coalesce(Listing.posted_at, Listing.first_seen)

_SORT_ORDERS = {
    "relevance": _DATE_KEY.desc(),  # no relevance score in DB; newest
    "recent": _DATE_KEY.desc(),
    "oldest": _DATE_KEY.asc(),
    "price_asc": nullslast(Listing.price.asc()),
    "price_desc": nullslast(Listing.price.desc()),
}


def _query_listings(
    session: Session,
    *,
    topic_id: int | None = None,
    tags: list[str] | None = None,
    source: str | None = None,
    status: str | None = None,
    keyword: str | None = None,
    price_min: float | None = None,
    price_max: float | None = None,
    distance_max: float | None = None,
    shipping: str | None = None,
    sort: str | None = None,
    limit: int,
    offset: int,
) -> list[Listing]:
    if topic_id is not None:
        stmt = (
            select(Listing)
            .join(ListingTopic, ListingTopic.listing_id == Listing.id)
            .where(ListingTopic.topic_id == topic_id)
        )
    else:
        stmt = select(Listing)

    if source:
        stmt = stmt.where(Listing.source == source)
    if status:
        stmt = stmt.where(Listing.status == status)
    else:
        stmt = stmt.where(Listing.status != ListingStatus.HIDDEN.value)
    if keyword:
        stmt = stmt.where(Listing.title.ilike(f"%{keyword}%"))
    if price_min is not None:
        stmt = stmt.where(Listing.price >= price_min)
    if price_max is not None:
        stmt = stmt.where(Listing.price <= price_max)
    if distance_max is not None:
        stmt = stmt.where(Listing.distance_km <= distance_max)

    order = _SORT_ORDERS.get(sort or "recent", _SORT_ORDERS["recent"])
    stmt = stmt.order_by(order).limit(limit).offset(offset)
    rows = list(session.scalars(stmt))

    if shipping:
        rows = [r for r in rows if shipping in (r.shipping_options or [])]

    if tags and topic_id is not None:
        tag_set = set(tags)
        filtered = []
        for r in rows:
            lt = session.scalar(
                select(ListingTopic).where(
                    ListingTopic.listing_id == r.id,
                    ListingTopic.topic_id == topic_id,
                )
            )
            if lt and tag_set.intersection(lt.tags or []):
                filtered.append(r)
        rows = filtered

    return rows


@router.get("/listings", response_model=list[ListingOut])
def list_listings(
    topic_id: int | None = None,
    source: str | None = None,
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
        topic_id=topic_id,
        source=source,
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


# ---------------------------------------------------------------------------
# Topics
# ---------------------------------------------------------------------------


@router.get("/topics", response_model=list[TopicOut])
def list_topics(session: Session = Depends(get_session)):
    return list(session.scalars(select(Topic).order_by(Topic.position, Topic.id)))


@router.post("/topics", response_model=TopicOut)
def create_topic(request_body: dict, session: Session = Depends(get_session)):
    name = request_body.get("name", "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Name is required")
    slug = _slugify(name)
    if session.scalar(select(Topic).where(Topic.slug == slug)):
        raise HTTPException(status_code=409, detail="Topic with this name already exists")
    topic = Topic(
        name=name,
        slug=slug,
        apprise_urls=request_body.get("apprise_urls", ""),
    )
    session.add(topic)
    session.commit()
    session.refresh(topic)
    return topic


@router.get("/topics/{slug}")
def get_topic(slug: str, session: Session = Depends(get_session)):
    topic = session.scalar(select(Topic).where(Topic.slug == slug))
    if topic is None:
        raise HTTPException(status_code=404, detail="Topic not found")
    count = session.scalar(
        select(func.count())
        .select_from(ListingTopic)
        .where(ListingTopic.topic_id == topic.id)
    )
    return {
        **TopicOut.model_validate(topic).model_dump(),
        "listing_count": count,
        "searches": [SavedSearchOut.model_validate(s).model_dump() for s in topic.searches],
    }


@router.put("/topics/{slug}", response_model=TopicOut)
def update_topic(slug: str, body: dict, session: Session = Depends(get_session)):
    topic = session.scalar(select(Topic).where(Topic.slug == slug))
    if topic is None:
        raise HTTPException(status_code=404, detail="Topic not found")
    for field in ("name", "apprise_urls"):
        if field in body:
            setattr(topic, field, body[field])
    if "name" in body:
        topic.slug = _slugify(body["name"])
    session.commit()
    session.refresh(topic)
    return topic


@router.delete("/topics/{slug}")
def delete_topic(slug: str, session: Session = Depends(get_session)):
    topic = session.scalar(select(Topic).where(Topic.slug == slug))
    if topic is None:
        raise HTTPException(status_code=404, detail="Topic not found")
    session.delete(topic)
    session.commit()
    return {"status": "deleted"}


# ---------------------------------------------------------------------------
# Searches within a topic
# ---------------------------------------------------------------------------


@router.post("/topics/{slug}/searches", response_model=SavedSearchOut)
def create_search(slug: str, body: dict, session: Session = Depends(get_session)):
    topic = session.scalar(select(Topic).where(Topic.slug == slug))
    if topic is None:
        raise HTTPException(status_code=404, detail="Topic not found")
    search = SavedSearch(
        topic_id=topic.id,
        name=body.get("name", body.get("query", "")[:64]),
        query=body.get("query", ""),
        price_max=body.get("price_max"),
        max_distance_km=body.get("max_distance_km"),
        sources=body.get("sources", []),
        tags=body.get("tags", []),
    )
    session.add(search)
    session.commit()
    session.refresh(search)
    return search


@router.delete("/topics/{slug}/searches/{search_id}")
def delete_search(slug: str, search_id: int, session: Session = Depends(get_session)):
    search = session.get(SavedSearch, search_id)
    if search is None:
        raise HTTPException(status_code=404, detail="Search not found")
    session.delete(search)
    session.commit()
    return {"status": "deleted"}


# ---------------------------------------------------------------------------
# Live search
# ---------------------------------------------------------------------------


@router.get("/search")
def live_search(
    q: str = "",
    price_max: float | None = None,
    distance_max: float | None = None,
    session: Session = Depends(get_session),
):
    if not q.strip():
        return []
    from .sources.base import SearchQuery
    from .sources.registry import get_enabled_sources

    query = SearchQuery(query=q, price_max=price_max, max_distance_km=distance_max)
    results = []
    for source in get_enabled_sources():
        try:
            results.extend(source.search(query))
        except Exception as exc:
            logger.warning("Live search: source %s failed: %s", source.name.value, exc)
    return [
        {
            "source": r.source.value,
            "source_id": r.source_id,
            "title": r.title,
            "url": r.url,
            "price": r.price,
            "currency": r.currency,
            "thumbnail": r.thumbnail,
            "location_city": r.location_city,
        }
        for r in results
    ]


# ---------------------------------------------------------------------------
# System
# ---------------------------------------------------------------------------


@router.post("/notify/test")
def notify_test():
    notifier = Notifier.from_settings()
    if not notifier.enabled:
        return {"enabled": False, "sent": False}
    return {"enabled": True, "sent": notifier.send_test()}


@router.post("/notify/test/{slug}")
def notify_test_topic(slug: str, session: Session = Depends(get_session)):
    topic = session.scalar(select(Topic).where(Topic.slug == slug))
    if topic is None:
        raise HTTPException(status_code=404, detail="Topic not found")
    notifier = Notifier.for_topic(topic.apprise_url_list)
    if not notifier.enabled:
        return {"enabled": False, "sent": False}
    return {"enabled": True, "sent": notifier.send_test(topic_name=topic.name)}


@router.post("/refresh", response_model=PollResultOut)
def refresh_now():
    result = poll_once()
    return PollResultOut(
        new_count=result.new_count,
        seen_count=result.seen_count,
        sources_run=result.sources_run,
        errors=result.errors,
    )


@router.post("/refresh/{slug}", response_model=PollResultOut)
def refresh_topic(slug: str, session: Session = Depends(get_session)):
    topic = session.scalar(select(Topic).where(Topic.slug == slug))
    if topic is None:
        raise HTTPException(status_code=404, detail="Topic not found")
    result = poll_topic(topic.id)
    return PollResultOut(
        new_count=result.new_count,
        seen_count=result.seen_count,
        sources_run=result.sources_run,
        errors=result.errors,
    )


@router.get("/settings")
def read_settings():
    s = get_settings()
    data = s.model_dump()
    data.pop("ebay_client_secret", None)
    return data


@router.put("/settings")
async def update_settings(request: Request):
    body = await request.json()
    old = get_settings()
    old_interval = old.poll_interval_minutes
    new_settings = save_settings(body)
    if new_settings.poll_interval_minutes != old_interval:
        reschedule_poll(new_settings.poll_interval_minutes)
    return {"status": "ok"}


import logging
logger = logging.getLogger(__name__)
