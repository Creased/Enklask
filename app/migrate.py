"""Idempotent database migration — runs on every startup before create_all."""

from __future__ import annotations

import logging

from sqlalchemy import inspect, text

from .db import SessionLocal, engine

logger = logging.getLogger(__name__)


def run_migration() -> None:
    _migrate_to_multi_topic()
    _ensure_saved_search_columns()
    _ensure_listing_columns()
    _drop_topic_columns()


def _ensure_listing_columns() -> None:
    """Idempotently add columns introduced after the multi-topic migration."""
    inspector = inspect(engine)
    if "listings" not in set(inspector.get_table_names()):
        return
    columns = {c["name"] for c in inspector.get_columns("listings")}
    if "price_history" not in columns:
        with engine.begin() as conn:
            conn.execute(
                text("ALTER TABLE listings ADD COLUMN price_history TEXT DEFAULT '[]'")
            )
        logger.info("Added 'price_history' column to listings.")


def _drop_topic_columns() -> None:
    """Drop the retired ``icon``/``color`` columns from topics (feature removed).

    SQLite 3.35+ supports ``DROP COLUMN``. Idempotent: only drops what's present.
    """
    inspector = inspect(engine)
    if "topics" not in set(inspector.get_table_names()):
        return
    columns = {c["name"] for c in inspector.get_columns("topics")}
    stale = [c for c in ("icon", "color") if c in columns]
    if not stale:
        return
    with engine.begin() as conn:
        for col in stale:
            conn.execute(text(f"ALTER TABLE topics DROP COLUMN {col}"))
    logger.info("Dropped retired topic columns: %s", ", ".join(stale))


def _ensure_saved_search_columns() -> None:
    """Idempotently add columns introduced after the multi-topic migration."""
    inspector = inspect(engine)
    if "saved_searches" not in set(inspector.get_table_names()):
        return
    columns = {c["name"] for c in inspector.get_columns("saved_searches")}
    if "condition" not in columns:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE saved_searches ADD COLUMN condition VARCHAR(16)"))
        logger.info("Added 'condition' column to saved_searches.")


def _migrate_to_multi_topic() -> None:
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())

    if "topics" in existing_tables:
        return

    needs_migration = "listings" in existing_tables or "saved_searches" in existing_tables

    if not needs_migration:
        logger.info("Fresh database — no migration needed.")
        return

    logger.info("Migrating existing data to multi-topic schema...")

    from .models import Base
    Base.metadata.create_all(engine)

    with SessionLocal() as session:
        ss_columns = {c["name"] for c in inspector.get_columns("saved_searches")}
        if "topic_id" not in ss_columns:
            session.execute(
                text("ALTER TABLE saved_searches ADD COLUMN topic_id INTEGER")
            )
        if "tags" not in ss_columns:
            session.execute(
                text("ALTER TABLE saved_searches ADD COLUMN tags TEXT DEFAULT '[]'")
            )
        session.commit()

        session.execute(
            text(
                "INSERT INTO topics (name, slug, apprise_urls, position, created_at) "
                "VALUES (:name, :slug, '', 0, CURRENT_TIMESTAMP)"
            ),
            {"name": "Imported", "slug": "imported"},
        )
        session.commit()

        topic_id = session.execute(
            text("SELECT id FROM topics WHERE slug = 'imported'")
        ).scalar_one()

        session.execute(
            text("UPDATE saved_searches SET topic_id = :tid WHERE topic_id IS NULL"),
            {"tid": topic_id},
        )
        session.commit()

        listing_ids = [
            row[0]
            for row in session.execute(text("SELECT id FROM listings")).fetchall()
        ]
        for lid in listing_ids:
            session.execute(
                text(
                    "INSERT OR IGNORE INTO listing_topics (listing_id, topic_id, search_id, tags) "
                    "VALUES (:lid, :tid, NULL, '[]')"
                ),
                {"lid": lid, "tid": topic_id},
            )
        session.commit()

        logger.info(
            "Migration complete: %d listings linked to 'Imported' topic.",
            len(listing_ids),
        )
