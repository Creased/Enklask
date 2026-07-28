"""Application settings, loaded from ``data/config.json``."""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any

from pydantic import BaseModel

logger = logging.getLogger(__name__)

CONFIG_PATH = Path("data/config.json")

_lock = threading.Lock()
_cached_settings: Settings | None = None


class Settings(BaseModel):
    # Storage
    database_url: str = "sqlite:///data/ad_tracker.db"

    # Scheduler
    poll_interval_minutes: int = 10

    # Home location (distance is measured from here). Default: Rennes.
    home_lat: float = 48.1173
    home_lon: float = -1.6778

    # Source toggles
    enable_ebay: bool = False
    enable_vinted: bool = False
    enable_leboncoin: bool = False

    # eBay Browse API (client-credentials OAuth)
    ebay_client_id: str = ""
    ebay_client_secret: str = ""
    ebay_marketplace_id: str = "EBAY_FR"

    # Vinted (unofficial)
    vinted_base_url: str = "https://www.vinted.fr"

    # Leboncoin (unofficial)
    leboncoin_base_url: str = "https://api.leboncoin.fr"

    # Shared cookies.txt file (Netscape format, e.g. from "Get cookies.txt LOCALLY").
    cookies_file: str = "data/cookies.txt"

    # Notifications (Apprise). One or more URLs, comma or whitespace separated.
    apprise_urls: str = ""
    notify_on_first_run: bool = False
    notify_max_per_poll: int = 15

    @property
    def apprise_url_list(self) -> list[str]:
        raw = self.apprise_urls.replace(",", " ")
        return [u.strip() for u in raw.split() if u.strip()]


def _load_from_file() -> dict[str, Any]:
    if CONFIG_PATH.exists():
        return json.loads(CONFIG_PATH.read_text("utf-8"))
    return {}


def _write_to_file(data: dict[str, Any]) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False), "utf-8")


def get_settings() -> Settings:
    global _cached_settings
    with _lock:
        if _cached_settings is None:
            raw = _load_from_file()
            _cached_settings = Settings(**raw)
            if not CONFIG_PATH.exists():
                _write_to_file(_cached_settings.model_dump())
        return _cached_settings


def save_settings(updates: dict[str, Any]) -> Settings:
    global _cached_settings
    with _lock:
        current = _load_from_file()
        valid_fields = set(Settings.model_fields)
        for key, value in updates.items():
            if key in valid_fields:
                current[key] = value
        new_settings = Settings(**current)
        _write_to_file(new_settings.model_dump())
        _cached_settings = new_settings
        return new_settings


def reload_settings() -> Settings:
    global _cached_settings
    with _lock:
        _cached_settings = None
    return get_settings()
