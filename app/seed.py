"""Seed sensible default saved searches for Switch repair hunting."""

from __future__ import annotations

from sqlalchemy import select

from .db import session_scope
from .models import SavedSearch

# Default queries tuned for Switch parts / for-parts hunting in France.
_DEFAULT_SEARCHES: list[dict] = [
    {"name": "Lot / pour pièces", "query": "nintendo switch lot pour pieces"},
    {"name": "Carte mère", "query": "nintendo switch carte mere"},
    {"name": "Châssis / coque", "query": "nintendo switch chassis coque"},
    {"name": "Écran OLED", "query": "nintendo switch oled ecran"},
    {"name": "Switch Lite HS", "query": "nintendo switch lite en panne hs"},
]


def seed_default_searches() -> None:
    """Insert default searches only if none exist yet."""
    with session_scope() as session:
        existing = session.scalar(select(SavedSearch).limit(1))
        if existing is not None:
            return
        for entry in _DEFAULT_SEARCHES:
            session.add(SavedSearch(name=entry["name"], query=entry["query"]))
