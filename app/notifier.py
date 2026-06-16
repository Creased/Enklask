"""Push notifications for new listings via Apprise.

Apprise routes a single notification to many services (Telegram, ntfy, Discord,
email, Pushover, Slack, ...) from simple URL strings, so the app stays agnostic
about where alerts go. Delivery is best-effort and fully isolated: a failure
here never affects polling or storage.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .config import Settings, get_settings

logger = logging.getLogger(__name__)


@dataclass
class NotifyItem:
    source: str
    title: str
    url: str
    topic_name: str = ""
    price: float | None = None
    currency: str = "EUR"
    location_city: str | None = None
    distance_km: float | None = None

    @classmethod
    def from_listing(cls, listing, topic_name: str = "") -> "NotifyItem":
        return cls(
            source=listing.source,
            title=listing.title,
            url=listing.url,
            topic_name=topic_name,
            price=listing.price,
            currency=listing.currency,
            location_city=listing.location_city,
            distance_km=listing.distance_km,
        )


class Notifier:
    def __init__(
        self,
        urls: list[str],
        max_per_poll: int = 15,
        notify_on_first_run: bool = False,
    ) -> None:
        self._urls = urls
        self._max_per_poll = max_per_poll
        self._notify_on_first_run = notify_on_first_run

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> "Notifier":
        settings = settings or get_settings()
        return cls(
            urls=settings.apprise_url_list,
            max_per_poll=settings.notify_max_per_poll,
            notify_on_first_run=settings.notify_on_first_run,
        )

    @classmethod
    def for_topic(cls, topic_urls: list[str], settings: Settings | None = None) -> "Notifier":
        settings = settings or get_settings()
        urls = topic_urls if topic_urls else settings.apprise_url_list
        return cls(
            urls=urls,
            max_per_poll=settings.notify_max_per_poll,
            notify_on_first_run=settings.notify_on_first_run,
        )

    @property
    def enabled(self) -> bool:
        return bool(self._urls)

    def format_listing(self, item: NotifyItem) -> tuple[str, str]:
        price = f"{item.price:.0f}{item.currency}" if item.price is not None else "?"
        prefix = f"[{item.topic_name}] " if item.topic_name else ""
        title = f"{prefix}{item.source} · {price} — {item.title[:80]}"

        lines: list[str] = []
        if item.location_city:
            loc = f"\U0001f4cd {item.location_city}"
            if item.distance_km is not None:
                loc += f" ({item.distance_km:.0f} km)"
            lines.append(loc)
        lines.append(item.url)
        return title, "\n".join(lines)

    def notify_new(self, items: list[NotifyItem], is_cold_start: bool = False) -> int:
        if not self.enabled or not items:
            return 0
        if is_cold_start and not self._notify_on_first_run:
            logger.info(
                "Skipping notifications for %d listings (first/seeding run).",
                len(items),
            )
            return 0

        if len(items) > self._max_per_poll:
            return 1 if self._send(*self._format_digest(items)) else 0

        sent = 0
        for item in items:
            title, body = self.format_listing(item)
            # No image attachment: the listing URL in the body already yields a
            # link preview (with the photo) on Telegram/Discord/etc.
            if self._send(title, body):
                sent += 1
        return sent

    def send_test(self, topic_name: str = "") -> bool:
        label = f"[{topic_name}] " if topic_name else ""
        return self._send(
            f"{label}Enklask",
            "Test de notification — si tu vois ça, Apprise est bien configuré ✅",
        )

    def _format_digest(self, items: list[NotifyItem]) -> tuple[str, str]:
        topic = items[0].topic_name if items[0].topic_name else "Enklask"
        title = f"[{topic}] {len(items)} nouvelles annonces"
        shown = items[: self._max_per_poll]
        lines = []
        for it in shown:
            price = f"{it.price:.0f}{it.currency}" if it.price is not None else "?"
            lines.append(f"• {price} — {it.title[:60]}\n  {it.url}")
        if len(items) > len(shown):
            lines.append(f"… et {len(items) - len(shown)} de plus")
        return title, "\n".join(lines)

    def _send(self, title: str, body: str) -> bool:
        try:
            import apprise
        except ImportError:
            logger.warning("apprise is not installed; cannot send notifications.")
            return False
        try:
            ap = apprise.Apprise()
            for url in self._urls:
                ap.add(url)
            return bool(ap.notify(title=title, body=body))
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException:
            logger.exception("Failed to send notification")
            return False
