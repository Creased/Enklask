"""Shared enumerations for sources and the Switch-parts taxonomy."""

from __future__ import annotations

from enum import Enum


class Source(str, Enum):
    EBAY = "ebay"
    VINTED = "vinted"
    LEBONCOIN = "leboncoin"
    FACEBOOK = "facebook"
    RAKUTEN = "rakuten"
    GEEV = "geev"


class ConsoleModel(str, Enum):
    V1 = "v1"  # original 2017 Switch
    V2 = "v2"  # 2019 refreshed Switch (better battery)
    LITE = "lite"
    OLED = "oled"
    UNKNOWN = "unknown"


class PartType(str, Enum):
    JOB_LOT = "job_lot"  # "lot de", bundles
    FOR_PARTS = "for_parts"  # "pour pièces", HS, en panne
    MOTHERBOARD = "motherboard"
    CHASSIS = "chassis"
    SCREEN = "screen"
    JOYCON = "joycon"
    BATTERY = "battery"
    OTHER = "other"


class ListingStatus(str, Enum):
    NEW = "new"
    SEEN = "seen"
    LIKED = "liked"
    HIDDEN = "hidden"
