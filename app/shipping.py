"""Detect shipping options mentioned in ad text."""

from __future__ import annotations

import re
import unicodedata


def _normalize(text: str) -> str:
    text = text or ""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", text.lower()).strip()


_SHIPPING_RULES: list[tuple[str, list[str]]] = [
    ("vinted_go", [r"\bvinted go\b"]),
    ("mondial_relay", [r"\bmondial relay\b", r"\bmondialrelay\b"]),
    ("pickup_point", [r"\bpoint relais\b", r"\brelais colis\b", r"\bpickup\b"]),
    ("hand_delivery", [r"\bremise en main propre\b", r"\bmain propre\b"]),
    ("colissimo", [r"\bcolissimo\b"]),
    ("chronopost", [r"\bchronopost\b"]),
]


def detect_shipping(*texts: str) -> list[str]:
    blob = _normalize(" ".join(t for t in texts if t))
    found: list[str] = []
    for label, patterns in _SHIPPING_RULES:
        if any(re.search(p, blob) for p in patterns):
            found.append(label)
    return found
