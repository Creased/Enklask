"""Builds the list of active source adapters from configuration.

Adapters are imported lazily so that an optional/heavy dependency (e.g.
Playwright for Facebook) never breaks startup when that source is disabled,
and so adapters added in later phases can be absent without errors.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from .base import BaseSource

logger = logging.getLogger(__name__)


def _ebay() -> BaseSource:
    from .ebay import EbaySource

    return EbaySource()


def _vinted() -> BaseSource:
    from .vinted import VintedSource

    return VintedSource()


def _leboncoin() -> BaseSource:
    from .leboncoin import LeboncoinSource

    return LeboncoinSource()


def _facebook() -> BaseSource:
    from .facebook import FacebookSource

    return FacebookSource()


def _rakuten() -> BaseSource:
    from .rakuten import RakutenSource

    return RakutenSource()


def _geev() -> BaseSource:
    from .geev import GeevSource

    return GeevSource()


_FACTORIES: tuple[Callable[[], BaseSource], ...] = (
    _ebay,
    _vinted,
    _leboncoin,
    _facebook,
    _rakuten,
    _geev,
)


def get_enabled_sources() -> list[BaseSource]:
    sources: list[BaseSource] = []
    for factory in _FACTORIES:
        try:
            source = factory()
        except ImportError:
            # Adapter not implemented yet / optional deps missing — skip quietly.
            logger.debug("Source adapter unavailable: %s", factory.__name__)
            continue
        except Exception:
            logger.exception("Failed to initialize source: %s", factory.__name__)
            continue
        if source.enabled:
            sources.append(source)
    return sources
