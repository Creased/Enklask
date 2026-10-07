"""Run saved searches against enabled sources and persist results."""

from __future__ import annotations

import logging
import random
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from sqlalchemy import func, select

from .db import session_scope
from .dedup import upsert_listing
from .models import Listing, ListingTopic, SavedSearch, Topic
from .notifier import Notifier, NotifyItem
from .sources.base import SearchQuery
from .sources.registry import get_enabled_sources

logger = logging.getLogger(__name__)

# Marketplaces occasionally omit an item for one response.  Requiring two
# complete, successful topic polls avoids hiding it on a transient miss while
# still removing sold/reserved/withdrawn ads promptly.
MISSING_POLLS_BEFORE_HIDING = 2


@dataclass
class PollResult:
    new_count: int = 0
    seen_count: int = 0
    errors: dict[str, str] = field(default_factory=dict)
    sources_run: list[str] = field(default_factory=list)


def poll_once() -> PollResult:
    """Execute every enabled SavedSearch across all topics."""
    result = PollResult()
    sources = get_enabled_sources()
    result.sources_run = [s.name.value for s in sources]
    if not sources:
        logger.info("No sources enabled — nothing to poll.")
        return result

    with session_scope() as session:
        topics = list(session.scalars(select(Topic)))

    if not topics:
        logger.info("No topics — nothing to poll.")
        return result

    is_cold_start = False
    with session_scope() as session:
        is_cold_start = session.scalar(select(func.count()).select_from(Listing)) == 0

    for topic in topics:
        topic_result = _poll_topic(topic, sources, is_cold_start)
        result.new_count += topic_result.new_count
        result.seen_count += topic_result.seen_count
        result.errors.update(topic_result.errors)

    logger.info(
        "Poll complete: %d new, %d seen, sources=%s, errors=%s",
        result.new_count,
        result.seen_count,
        result.sources_run,
        result.errors,
    )
    return result


def poll_topic(topic_id: int) -> PollResult:
    """Poll a single topic."""
    sources = get_enabled_sources()
    if not sources:
        return PollResult()

    with session_scope() as session:
        topic = session.get(Topic, topic_id)
        if topic is None:
            return PollResult()
        is_cold_start = (
            session.scalar(
                select(func.count())
                .select_from(ListingTopic)
                .where(ListingTopic.topic_id == topic_id)
            )
            == 0
        )

    return _poll_topic(topic, sources, is_cold_start)


def poll_search(search_id: int) -> PollResult:
    """Poll a single SavedSearch (refreshes just that search)."""
    sources = get_enabled_sources()
    result = PollResult()
    result.sources_run = [s.name.value for s in sources]
    if not sources:
        return result

    with session_scope() as session:
        search = session.get(SavedSearch, search_id)
        if search is None or not search.enabled:
            return result
        topic = session.get(Topic, search.topic_id)
        if topic is None:
            return result
        # Touch attributes so they stay usable after the session closes.
        _ = (search.query, search.price_min, search.price_max, search.max_distance_km,
             search.condition, search.sources, search.tags, search.id,
             topic.id, topic.name, topic.apprise_url_list)
        is_cold_start = (
            session.scalar(
                select(func.count())
                .select_from(ListingTopic)
                .where(ListingTopic.topic_id == topic.id)
            )
            == 0
        )

    (
        new_count,
        seen_count,
        errors,
        new_items,
        _,
        _,
        _,
    ) = _poll_one(topic, search, sources)
    result.new_count = new_count
    result.seen_count = seen_count
    result.errors.update(errors)
    _notify(topic, new_items, is_cold_start)
    return result


def _poll_topic(topic, sources, is_cold_start: bool) -> PollResult:
    result = PollResult()
    result.sources_run = [s.name.value for s in sources]

    with session_scope() as session:
        searches = list(
            session.scalars(
                select(SavedSearch).where(
                    SavedSearch.topic_id == topic.id,
                    SavedSearch.enabled.is_(True),
                )
            )
        )

    if not searches:
        return result

    new_items: list[NotifyItem] = []
    attempted: Counter[str] = Counter()
    successful: Counter[str] = Counter()
    seen_by_source: dict[str, set[str]] = defaultdict(set)
    for search in searches:
        (
            new_count,
            seen_count,
            errors,
            items,
            source_ids,
            attempted_sources,
            successful_sources,
        ) = _poll_one(topic, search, sources)
        result.new_count += new_count
        result.seen_count += seen_count
        result.errors.update(errors)
        new_items.extend(items)
        attempted.update(attempted_sources)
        successful.update(successful_sources)
        for source_name, ids in source_ids.items():
            seen_by_source[source_name].update(ids)

    # Do not hide anything when one query failed: the union would be incomplete.
    for source_name, attempt_count in attempted.items():
        ids = seen_by_source[source_name]
        if successful[source_name] == attempt_count:
            _reconcile_availability(topic.id, source_name, ids)

    _notify(topic, new_items, is_cold_start)
    return result


def _poll_one(topic, search, sources) -> tuple[
    int, int, dict, list, dict[str, set[str]], set[str], set[str]
]:
    """Run one search and return counts plus per-source reconciliation data."""
    new_count = seen_count = 0
    errors: dict[str, str] = {}
    new_items: list[NotifyItem] = []
    source_ids: dict[str, set[str]] = {}
    attempted_sources: set[str] = set()
    successful_sources: set[str] = set()

    query = SearchQuery(
        query=search.query,
        price_min=search.price_min,
        price_max=search.price_max,
        max_distance_km=search.max_distance_km,
        condition=search.condition,
    )
    targeted = set(search.sources or [])
    for source in sources:
        if targeted and source.name.value not in targeted:
            continue
        source_name = source.name.value
        attempted_sources.add(source_name)
        try:
            raws = source.search(query)
        except Exception as exc:
            logger.warning("Source %s failed: %s", source_name, exc)
            errors[source_name] = str(exc)
            continue
        successful_sources.add(source_name)
        source_ids[source_name] = {raw.source_id for raw in raws}

        with session_scope() as session:
            for raw in raws:
                listing = upsert_listing(
                    session,
                    raw,
                    topic_id=topic.id,
                    search_id=search.id,
                    tags=search.tags or [],
                )
                if listing is not None:
                    new_count += 1
                    new_items.append(
                        NotifyItem.from_listing(listing, topic_name=topic.name)
                    )
                else:
                    seen_count += 1

        time.sleep(random.uniform(0.5, 1.5))

    return (
        new_count,
        seen_count,
        errors,
        new_items,
        source_ids,
        attempted_sources,
        successful_sources,
    )


def _mark_missing_links(
    session, topic_id: int, source_name: str, seen_source_ids: set[str]
) -> None:
    """Advance availability for ads absent from a successful source refresh."""
    links = list(
        session.scalars(
            select(ListingTopic)
            .join(Listing, Listing.id == ListingTopic.listing_id)
            .where(
                ListingTopic.topic_id == topic_id,
                Listing.source == source_name,
            )
        )
    )
    for link in links:
        if link.listing.source_id not in seen_source_ids:
            link.missed_polls += 1
            if link.missed_polls >= MISSING_POLLS_BEFORE_HIDING:
                link.is_available = False


def _reconcile_availability(
    topic_id: int, source_name: str, seen_source_ids: set[str]
) -> None:
    with session_scope() as session:
        _mark_missing_links(session, topic_id, source_name, seen_source_ids)


def _notify(topic, new_items: list[NotifyItem], is_cold_start: bool) -> None:
    try:
        notifier = Notifier.for_topic(topic.apprise_url_list)
        notifier.notify_new(new_items, is_cold_start=is_cold_start)
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:
        logger.exception("Notification step failed for topic '%s'", topic.name)
