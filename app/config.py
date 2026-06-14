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


@lru_cache
def get_settings() -> Settings:
    return Settings()
