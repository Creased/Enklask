"""Shared enumerations."""

from __future__ import annotations

from enum import Enum


class Source(str, Enum):
    EBAY = "ebay"
    VINTED = "vinted"
    LEBONCOIN = "leboncoin"


class ListingStatus(str, Enum):
    NEW = "new"
    SEEN = "seen"
    LIKED = "liked"
    HIDDEN = "hidden"
