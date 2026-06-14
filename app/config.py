"""Application settings, loaded from environment / `.env`."""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # Storage
    database_url: str = "sqlite:///data/ad_tracker.db"

    # Scheduler
    poll_interval_minutes: int = 10

    # Home location (distance is measured from here). Default: Rennes.
    home_lat: float = 48.1173
    home_lon: float = -1.6778

    # Source toggles
    enable_ebay: bool = True
    enable_vinted: bool = False
    enable_leboncoin: bool = False
    enable_facebook: bool = False

    # eBay Browse API (client-credentials OAuth)
    ebay_client_id: str = ""
    ebay_client_secret: str = ""
    ebay_marketplace_id: str = "EBAY_FR"

    # Vinted (unofficial)
    vinted_base_url: str = "https://www.vinted.fr"

    # Leboncoin (unofficial)
    leboncoin_base_url: str = "https://api.leboncoin.fr"

    # Facebook Marketplace (experimental)
    facebook_storage_state: str = "data/facebook_state.json"
    facebook_city: str = "rennes"

    # Notifications (Apprise). One or more URLs, comma or whitespace separated.
    apprise_urls: str = ""
    notify_on_first_run: bool = False
    notify_max_per_poll: int = 15

    @property
    def apprise_url_list(self) -> list[str]:
        """Parse ``apprise_urls`` into a clean list (comma/whitespace separated)."""
        raw = self.apprise_urls.replace(",", " ")
        return [u.strip() for u in raw.split() if u.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
