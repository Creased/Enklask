"""Background scheduler that polls sources on an interval."""

from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from .config import get_settings
from .poller import poll_once

logger = logging.getLogger(__name__)

_scheduler: BackgroundScheduler | None = None


def start_scheduler() -> BackgroundScheduler:
    global _scheduler
    if _scheduler is not None:
        return _scheduler

    settings = get_settings()
    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(
        _safe_poll,
        trigger=IntervalTrigger(minutes=settings.poll_interval_minutes),
        id="poll_sources",
        max_instances=1,
        coalesce=True,
        # Random jitter so calls don't hit marketplaces at the exact same second.
        jitter=60,
    )
    scheduler.start()
    _scheduler = scheduler
    logger.info(
        "Scheduler started: polling every %d min", settings.poll_interval_minutes
    )
    return scheduler


def shutdown_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None


def reschedule_poll(interval_minutes: int) -> None:
    if _scheduler is None:
        return
    _scheduler.reschedule_job(
        "poll_sources",
        trigger=IntervalTrigger(minutes=interval_minutes),
    )
    logger.info("Scheduler rescheduled: polling every %d min", interval_minutes)


def _safe_poll() -> None:
    try:
        poll_once()
    except Exception:  # pragma: no cover - scheduler must never die
        logger.exception("Scheduled poll failed")
