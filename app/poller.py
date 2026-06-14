"""Run saved searches against enabled sources and persist results."""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, field

from sqlalchemy import select

from .db import session_scope
from .dedup import upsert_listing
from .models import SavedSearch
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
                    if upsert_listing(session, raw):
                        result.new_count += 1
                    else:
                        result.seen_count += 1

            # Be polite: small jittered pause between source calls.
            time.sleep(random.uniform(0.5, 1.5))

    logger.info(
        "Poll complete: %d new, %d seen, sources=%s, errors=%s",
        result.new_count,
        result.seen_count,
        result.sources_run,
        result.errors,
    )
    return result
