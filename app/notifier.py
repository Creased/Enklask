"""Push notifications for new listings.

Apprise routes a single notification to many services (Telegram, ntfy, email,
Pushover, Slack, ...) from simple URL strings, so the app stays agnostic about
where alerts go. For **Discord** webhooks we bypass Apprise's generic formatter
and POST a rich embed directly (title + description + price/source/location
fields + the listing photo as a thumbnail, à la Seerr) — Apprise can't set an
arbitrary embed thumbnail. Everything else still goes through Apprise as text.

Delivery is best-effort and fully isolated: a failure here never affects polling
or storage.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import parse_qs, unquote, urlsplit

import httpx

from .config import Settings, get_settings

logger = logging.getLogger(__name__)

# Embed accent colour per source (matches the web UI's source pills).
_SOURCE_COLOR = {
    "ebay": 0x0064D2,
    "vinted": 0x09B1BA,
    "leboncoin": 0xFF6E14,
}
_DEFAULT_COLOR = 0xE63946  # brand red

_DISCORD_WEBHOOK_RE = re.compile(
    r"https?://(?:ptb\.|canary\.)?discord(?:app)?\.com/api/webhooks/(\d+)/([\w-]+)", re.I
)


def parse_discord_target(url: str) -> dict | None:
    """Parse an Apprise ``discord://[botname@]id/token`` URL (or a raw Discord
    webhook URL) into ``{webhook, username, avatar_url}``. The username/avatar
    come from the ``discord://`` botname and ``?avatar_url=`` so posting directly
    keeps the same bot identity Apprise used. Returns None if it isn't Discord.
    """
    s = (url or "").strip()
    u = urlsplit(s)
    scheme = u.scheme.lower()
    webhook = username = None
    if scheme in ("discord", "discordapp"):
        userinfo, _, host = u.netloc.rpartition("@")
        username = userinfo or None
        ids = [p for p in ([host] + u.path.split("/")) if p]
        if len(ids) >= 2 and ids[0].isdigit():
            webhook = f"https://discord.com/api/webhooks/{ids[0]}/{ids[1]}"
    elif scheme in ("http", "https"):
        m = _DISCORD_WEBHOOK_RE.match(s)
        if m:
            webhook = f"https://discord.com/api/webhooks/{m.group(1)}/{m.group(2)}"
    if not webhook:
        return None

    q = parse_qs(u.query)
    avatar_url = (q.get("avatar_url") or [None])[0]
    if avatar_url:
        avatar_url = unquote(avatar_url)
    if not username:
        username = (q.get("username") or [None])[0]
    return {"webhook": webhook, "username": username, "avatar_url": avatar_url}


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
    description: str = ""
    thumbnail: str | None = None

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
            description=listing.description or "",
            thumbnail=listing.thumbnail,
        )


class Notifier:
    def __init__(
        self,
        urls: list[str],
        max_per_poll: int = 15,
        notify_on_first_run: bool = False,
    ) -> None:
        self._urls = urls
        # Split Discord webhooks (rich embeds) from everything else (Apprise text).
        self._discord: list[dict] = []
        self._other: list[str] = []
        for u in urls:
            target = parse_discord_target(u)
            if target:
                self._discord.append(target)
            else:
                self._other.append(u)
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

    def build_embed(self, item: NotifyItem) -> dict:
        """A Seerr-style Discord embed: clickable title, description (if any),
        price/source/location fields, and the listing photo as a thumbnail."""
        price = f"{item.price:.0f} {item.currency}" if item.price is not None else "?"
        fields = [
            {"name": "Prix", "value": price, "inline": True},
            {"name": "Source", "value": item.source, "inline": True},
        ]
        if item.location_city or item.distance_km is not None:
            loc = item.location_city or ""
            if item.distance_km is not None:
                loc = f"{loc} ({item.distance_km:.0f} km)".strip()
            fields.append({"name": "Lieu", "value": loc or "—", "inline": True})

        embed: dict = {
            "title": item.title[:256],
            "url": item.url,
            "color": _SOURCE_COLOR.get(item.source, _DEFAULT_COLOR),
            "fields": fields,
            "footer": {"text": item.topic_name or "Enklask"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        if item.description:
            embed["description"] = item.description[:600]
        if item.thumbnail:
            embed["thumbnail"] = {"url": item.thumbnail}
        return embed

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
            if self._send_listing(item):
                sent += 1
        return sent

    def send_test(self, topic_name: str = "") -> bool:
        ok = False
        if self._discord:
            sample = NotifyItem(
                source="vinted",
                title="Nintendo Switch Lite grise (HS) — exemple",
                url="https://www.vinted.fr/",
                topic_name=topic_name,
                price=50.0,
                currency="EUR",
                location_city="Rennes",
                distance_km=3.0,
                description="Test d'aperçu — si tu vois cette carte, les notifications "
                "Discord sont prêtes ✅ (la photo de l'annonce s'affichera ici).",
            )
            embed = self.build_embed(sample)
            for target in self._discord:
                ok = self._post_discord(target, embed) or ok
        if self._other:
            label = f"[{topic_name}] " if topic_name else ""
            ok = self._send(
                f"{label}Enklask",
                "Test de notification — si tu vois ça, Apprise est bien configuré ✅",
                urls=self._other,
            ) or ok
        return ok

    def _send_listing(self, item: NotifyItem) -> bool:
        """Rich embed to Discord webhooks; Apprise text to everything else."""
        sent = False
        if self._discord:
            embed = self.build_embed(item)
            for target in self._discord:
                sent = self._post_discord(target, embed) or sent
        if self._other:
            title, body = self.format_listing(item)
            sent = self._send(title, body, urls=self._other) or sent
        return sent

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

    def _post_discord(self, target: dict, embed: dict) -> bool:
        payload: dict = {"embeds": [embed]}
        if target.get("username"):
            payload["username"] = target["username"]
        if target.get("avatar_url"):
            payload["avatar_url"] = target["avatar_url"]
        try:
            resp = httpx.post(target["webhook"], json=payload, timeout=15.0)
            if resp.status_code in (200, 204):
                return True
            logger.warning(
                "Discord webhook returned %s: %s", resp.status_code, resp.text[:200]
            )
            return False
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException:
            logger.exception("Discord webhook post failed")
            return False

    def _send(self, title: str, body: str, urls: list[str] | None = None) -> bool:
        targets = urls if urls is not None else self._urls
        if not targets:
            return False
        try:
            import apprise
        except ImportError:
            logger.warning("apprise is not installed; cannot send notifications.")
            return False
        try:
            ap = apprise.Apprise()
            for url in targets:
                ap.add(url)
            return bool(ap.notify(title=title, body=body))
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException:
            logger.exception("Failed to send notification")
            return False
