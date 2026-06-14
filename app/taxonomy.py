"""Classify free-text marketplace ads into Switch model + part type.

Pure functions, reused by every source adapter and unit-tested. The rules are
keyword/regex based and tuned for French (and some English) second-hand ads.
"""

from __future__ import annotations

import re
import unicodedata

from .enums import ConsoleModel, PartType


def _normalize(text: str) -> str:
    """Lowercase, strip accents, collapse whitespace for robust matching."""
    text = text or ""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", text.lower()).strip()


# Order matters: more specific models are checked first (OLED / Lite before the
# generic V1/V2 detection, which is itself fuzzy).
_MODEL_RULES: list[tuple[ConsoleModel, list[str]]] = [
    (ConsoleModel.OLED, [r"\boled\b"]),
    (ConsoleModel.LITE, [r"\blite\b"]),
    (
        ConsoleModel.V2,
        [
            r"\bv2\b",
            r"\bhac-001-?01\b",
            r"\bnouveau modele\b",
            r"\bnew model\b",
            r"\bameliore[e]?\b",
            r"\bautonomie amelioree\b",
        ],
    ),
    (
        ConsoleModel.V1,
        [
            r"\bv1\b",
            r"\bhac-001\b(?!-01)",
            r"\bpremiere generation\b",
            r"\b1ere generation\b",
            r"\bmodele original\b",
            r"\boriginal model\b",
        ],
    ),
]

# Part rules. Checked top to bottom; first match wins. "for parts" / job lot are
# strong signals that should win over a generic component mention.
_PART_RULES: list[tuple[PartType, list[str]]] = [
    (
        PartType.JOB_LOT,
        [r"\blot de\b", r"\blot \d", r"\bjob lot\b", r"\bbundle\b", r"\bensemble de\b"],
    ),
    (
        PartType.FOR_PARTS,
        [
            r"\bpour pieces?\b",
            r"\bpour piece detachee\b",
            r"\bpieces detachees\b",
            r"\bfor parts\b",
            r"\bspares? or repairs?\b",
            r"\ben panne\b",
            r"\bhs\b",
            r"\bne s'allume plus\b",
            r"\bdefectueux\b",
            r"\bne fonctionne pas\b",
            r"\bnot working\b",
            r"\bfaulty\b",
        ],
    ),
    (
        PartType.MOTHERBOARD,
        [
            r"\bcarte mere\b",
            r"\bmotherboard\b",
            r"\bmainboard\b",
            r"\bpcb\b",
        ],
    ),
    (
        PartType.SCREEN,
        [
            r"\becran\b",
            r"\bdalle\b",
            r"\bvitre\b",
            r"\bscreen\b",
            r"\blcd\b",
            r"\bdigitizer\b",
        ],
    ),
    (
        PartType.CHASSIS,
        [
            r"\bchassis\b",
            r"\bcoque\b",
            r"\bboitier\b",
            r"\bhousing\b",
            r"\bshell\b",
            r"\bback plate\b",
            r"\bplasturgie\b",
        ],
    ),
    (
        PartType.JOYCON,
        [r"\bjoy-?con", r"\bmanette", r"\bcontroller\b", r"\bstick\b"],
    ),
    (
        PartType.BATTERY,
        [r"\bbatterie\b", r"\bbattery\b", r"\baccu\b"],
    ),
]


def _matches(text: str, patterns: list[str]) -> bool:
    return any(re.search(p, text) for p in patterns)


def classify_model(title: str, description: str = "") -> ConsoleModel:
    text = _normalize(f"{title} {description}")
    for model, patterns in _MODEL_RULES:
        if _matches(text, patterns):
            return model
    return ConsoleModel.UNKNOWN


def classify_part(title: str, description: str = "") -> PartType:
    text = _normalize(f"{title} {description}")
    for part, patterns in _PART_RULES:
        if _matches(text, patterns):
            return part
    return PartType.OTHER


def classify(title: str, description: str = "") -> tuple[ConsoleModel, PartType]:
    return classify_model(title, description), classify_part(title, description)


# --- Shipping detection -----------------------------------------------------

_SHIPPING_RULES: list[tuple[str, list[str]]] = [
    ("vinted_go", [r"\bvinted go\b"]),
    ("mondial_relay", [r"\bmondial relay\b", r"\bmondialrelay\b"]),
    ("pickup_point", [r"\bpoint relais\b", r"\brelais colis\b", r"\bpickup\b"]),
    ("hand_delivery", [r"\bremise en main propre\b", r"\bmain propre\b"]),
    ("colissimo", [r"\bcolissimo\b"]),
    ("chronopost", [r"\bchronopost\b"]),
]


def detect_shipping(*texts: str) -> list[str]:
    """Best-effort extraction of shipping options mentioned in ad text."""
    blob = _normalize(" ".join(t for t in texts if t))
    found: list[str] = []
    for label, patterns in _SHIPPING_RULES:
        if _matches(blob, patterns):
            found.append(label)
    return found
