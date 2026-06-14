"""SQLAlchemy ORM models."""

from __future__ import annotations

import re
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from .enums import ListingStatus, Source


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _slugify(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    return re.sub(r"[-\s]+", "-", text)[:128]


class Base(DeclarativeBase):
    pass


class Topic(Base):
    __tablename__ = "topics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128))
    slug: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    apprise_urls: Mapped[str] = mapped_column(Text, default="")
    position: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    searches: Mapped[list["SavedSearch"]] = relationship(
        back_populates="topic", cascade="all, delete-orphan"
    )
    listing_links: Mapped[list["ListingTopic"]] = relationship(
        back_populates="topic", cascade="all, delete-orphan"
    )

    @property
    def apprise_url_list(self) -> list[str]:
        raw = (self.apprise_urls or "").replace(",", " ")
        return [u.strip() for u in raw.split() if u.strip()]

    @property
    def all_tags(self) -> list[str]:
        seen: set[str] = set()
        tags: list[str] = []
        for s in self.searches:
            for t in (s.tags or []):
                if t not in seen:
                    seen.add(t)
                    tags.append(t)
        return tags


class ListingTopic(Base):
    __tablename__ = "listing_topics"

    listing_id: Mapped[int] = mapped_column(
        ForeignKey("listings.id", ondelete="CASCADE"), primary_key=True
    )
    topic_id: Mapped[int] = mapped_column(
        ForeignKey("topics.id", ondelete="CASCADE"), primary_key=True
    )
    search_id: Mapped[int | None] = mapped_column(
        ForeignKey("saved_searches.id", ondelete="SET NULL"), nullable=True
    )
    tags: Mapped[list] = mapped_column(JSON, default=list)

    listing: Mapped["Listing"] = relationship(back_populates="topic_links")
    topic: Mapped["Topic"] = relationship(back_populates="listing_links")


class Listing(Base):
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
    # Price snapshots over time, each {"price": float, "at": iso}. Grows when a
    # refresh sees a changed price, so drops/rises can be shown on the card.
    price_history: Mapped[list] = mapped_column(JSON, default=list)

    thumbnail: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    photos: Mapped[list] = mapped_column(JSON, default=list)

    location_city: Mapped[str | None] = mapped_column(String(128), nullable=True)
    distance_km: Mapped[float | None] = mapped_column(Float, nullable=True)
    shipping_options: Mapped[list] = mapped_column(JSON, default=list)

    status: Mapped[str] = mapped_column(
        String(12), default=ListingStatus.NEW.value, index=True
    )

    posted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    first_seen: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    last_seen: Mapped[datetime] = mapped_column(
        DateTime, default=_utcnow, onupdate=_utcnow
    )

    topic_links: Mapped[list["ListingTopic"]] = relationship(
        back_populates="listing", cascade="all, delete-orphan"
    )


class SavedSearch(Base):
    __tablename__ = "saved_searches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    topic_id: Mapped[int] = mapped_column(
        ForeignKey("topics.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(128))
    query: Mapped[str] = mapped_column(String(512), default="")
    price_max: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_distance_km: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Generic condition bucket (new/like_new/good/fair/for_parts); None = any.
    condition: Mapped[str | None] = mapped_column(String(16), nullable=True)
    sources: Mapped[list] = mapped_column(JSON, default=list)
    tags: Mapped[list] = mapped_column(JSON, default=list)
    enabled: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    topic: Mapped["Topic"] = relationship(back_populates="searches")


__all__ = [
    "Base",
    "Topic",
    "ListingTopic",
    "Listing",
    "SavedSearch",
    "Source",
    "ListingStatus",
]
