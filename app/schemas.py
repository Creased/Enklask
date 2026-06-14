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
    model_guess: str
    part_guess: str
    status: str
    posted_at: datetime | None
    first_seen: datetime


class PollResultOut(BaseModel):
    new_count: int
    seen_count: int
    sources_run: list[str]
    errors: dict[str, str]
