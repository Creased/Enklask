"""Database engine and session helpers."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from .config import get_settings


def _make_engine():
    settings = get_settings()
    url = settings.database_url
    # Ensure the parent directory exists for file-based SQLite databases.
    if url.startswith("sqlite:///"):
        path = url.removeprefix("sqlite:///")
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    return create_engine(url, connect_args=connect_args, future=True)


engine = _make_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def init_db() -> None:
    """Create all tables. Safe to call repeatedly."""
    from . import models  # noqa: F401  (register mappers)

    models.Base.metadata.create_all(bind=engine)


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional session context manager."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_session() -> Iterator[Session]:
    """FastAPI dependency that yields a session."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
