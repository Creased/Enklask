"""Pydantic DTOs returned by the JSON API."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class ListingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    source: str
    title: str
    url: str
    price: float | None
    currency: str
    thumbnail: str | None
    photos: list
    location_city: str | None
    distance_km: float | None
    shipping_options: list
    shipping_cost: float | None
    buying_format: str | None
    status: str
    posted_at: datetime | None
    first_seen: datetime


class TopicOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    slug: str
    apprise_urls: str
    position: int
    created_at: datetime


class SavedSearchOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    topic_id: int
    name: str
    query: str
    price_max: float | None
    max_distance_km: float | None
    sources: list
    tags: list
    enabled: bool
    created_at: datetime


class PollResultOut(BaseModel):
    new_count: int
    seen_count: int
    sources_run: list[str]
    errors: dict[str, str]
