"""SQLAlchemy ORM models."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from .enums import ConsoleModel, ListingStatus, PartType, Source


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Listing(Base):
    """A normalized ad from any marketplace."""

    __tablename__ = "listings"
    __table_args__ = (
        UniqueConstraint("source", "source_id", name="uq_source_item"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    source: Mapped[str] = mapped_column(String(20), index=True)
    source_id: Mapped[str] = mapped_column(String(128), index=True)

    title: Mapped[str] = mapped_column(String(512))
    description: Mapped[str] = mapped_column(Text, default="")
    url: Mapped[str] = mapped_column(String(1024))

    price: Mapped[float | None] = mapped_column(Float, nullable=True)
    currency: Mapped[str] = mapped_column(String(8), default="EUR")

    thumbnail: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    photos: Mapped[list] = mapped_column(JSON, default=list)

    location_city: Mapped[str | None] = mapped_column(String(128), nullable=True)
    distance_km: Mapped[float | None] = mapped_column(Float, nullable=True)
    shipping_options: Mapped[list] = mapped_column(JSON, default=list)

    model_guess: Mapped[str] = mapped_column(
        String(16), default=ConsoleModel.UNKNOWN.value, index=True
    )
    part_guess: Mapped[str] = mapped_column(
        String(16), default=PartType.OTHER.value, index=True
    )

    status: Mapped[str] = mapped_column(
        String(12), default=ListingStatus.NEW.value, index=True
    )

    posted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    first_seen: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    last_seen: Mapped[datetime] = mapped_column(
        DateTime, default=_utcnow, onupdate=_utcnow
    )


class SavedSearch(Base):
    """A reusable query run against the enabled sources."""

    __tablename__ = "saved_searches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128))
    query: Mapped[str] = mapped_column(String(512), default="")
    price_max: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_distance_km: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Which sources this search targets; empty list means "all enabled".
    sources: Mapped[list] = mapped_column(JSON, default=list)
    enabled: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


# Re-export for convenience.
__all__ = [
    "Base",
    "Listing",
    "SavedSearch",
    "Source",
    "ConsoleModel",
    "PartType",
    "ListingStatus",
]
