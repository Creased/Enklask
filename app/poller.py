"""Run saved searches against enabled sources and persist results."""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, field

from sqlalchemy import func, select

from .db import session_scope
from .dedup import upsert_listing
from .models import Listing, SavedSearch
from .notifier import Notifier, NotifyItem
from .sources.base import SearchQuery
from .sources.registry import get_enabled_sources

logger = logging.getLogger(__name__)


@dataclass
class PollResult:
    new_count: int = 0
    seen_count: int = 0
    errors: dict[str, str] = field(default_factory=dict)
    sources_run: list[str] = field(default_factory=list)


def poll_once() -> PollResult:
    """Execute every enabled SavedSearch against every enabled source."""
    result = PollResult()
    sources = get_enabled_sources()
    result.sources_run = [s.name.value for s in sources]
    if not sources:
        logger.info("No sources enabled — nothing to poll.")
        return result

    with session_scope() as session:
        searches = list(
            session.scalars(select(SavedSearch).where(SavedSearch.enabled.is_(True)))
        )
        # A cold start (empty DB) is a seeding run — don't blast notifications.
        is_cold_start = session.scalar(select(func.count()).select_from(Listing)) == 0

    new_items: list[NotifyItem] = []
    for search in searches:
        query = SearchQuery(
            query=search.query,
            price_max=search.price_max,
            max_distance_km=search.max_distance_km,
        )
        targeted = set(search.sources or [])
        for source in sources:
            if targeted and source.name.value not in targeted:
                continue
            try:
                raws = source.search(query)
            except Exception as exc:  # isolate per-source failures
                logger.warning("Source %s failed: %s", source.name.value, exc)
                result.errors[source.name.value] = str(exc)
                continue

            with session_scope() as session:
                for raw in raws:
                    listing = upsert_listing(session, raw)
                    if listing is not None:
                        result.new_count += 1
                        # Snapshot inside the session for later notification.
                        new_items.append(NotifyItem.from_listing(listing))
                    else:
                        result.seen_count += 1

            # Be polite: small jittered pause between source calls.
            time.sleep(random.uniform(0.5, 1.5))

    try:
        Notifier.from_settings().notify_new(new_items, is_cold_start=is_cold_start)
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:  # notifications must never break polling
        logger.exception("Notification step failed")

    logger.info(
        "Poll complete: %d new, %d seen, sources=%s, errors=%s",
        result.new_count,
        result.seen_count,
        result.sources_run,
        result.errors,
    )
    return result
