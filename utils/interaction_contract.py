"""Shared interaction payload contract helpers.

The UI, DB sync layer, validators, and prompts all need the same small
vocabulary for arrows and claim directions. Keep the rules here so a
future wording tweak does not reopen the scalar/JSONB/frontend drift we
just cleaned up.
"""

from __future__ import annotations

from typing import Any, Iterable


CANONICAL_ARROWS = ("activates", "inhibits", "binds", "regulates")
CANONICAL_ARROW_SET = set(CANONICAL_ARROWS)

SEMANTIC_DIRECTIONS = ("main_to_primary", "primary_to_main")
SEMANTIC_DIRECTION_SET = set(SEMANTIC_DIRECTIONS)


_ARROW_SYNONYMS = {
    "activate": "activates",
    "activated": "activates",
    "activation": "activates",
    "promotes": "activates",
    "enhances": "activates",
    "stimulates": "activates",
    "upregulates": "activates",
    "increases": "activates",
    "inhibit": "inhibits",
    "inhibited": "inhibits",
    "inhibition": "inhibits",
    "suppresses": "inhibits",
    "represses": "inhibits",
    "blocks": "inhibits",
    "downregulates": "inhibits",
    "decreases": "inhibits",
    "binding": "binds",
    "bound": "binds",
    "interacts": "binds",
    "associates": "binds",
    "complex": "binds",
    "complexes": "binds",
    "complex formation": "binds",
    "forms complex": "binds",
    "modulates": "regulates",
    "modulation": "regulates",
    "affects": "regulates",
    "unknown": "regulates",
    "unk": "regulates",
    "none": "regulates",
    "null": "regulates",
}


def normalize_arrow(value: Any, default: str = "regulates") -> str:
    """Return one of ``CANONICAL_ARROWS``.

    ``complex`` is normalized to ``binds`` because in this app it means a
    physical/co-complex relationship, not a fifth visual arrow class.
    Unknown or empty values fall back to ``default``.
    """

    raw = str(value or "").strip().lower()
    if not raw:
        return default
    if raw in CANONICAL_ARROW_SET:
        return raw
    if raw in _ARROW_SYNONYMS:
        return _ARROW_SYNONYMS[raw]
    if "activ" in raw or "promot" in raw or "enhanc" in raw or "stimulat" in raw:
        return "activates"
    if "inhib" in raw or "suppress" in raw or "repress" in raw or "degrad" in raw:
        return "inhibits"
    if "bind" in raw or "complex" in raw or "interact" in raw or "associat" in raw:
        return "binds"
    if "regulat" in raw or "modulat" in raw:
        return "regulates"
    return default


def normalize_arrow_list(values: Iterable[Any] | None, default: str = "regulates") -> list[str]:
    """Normalize, dedupe, and preserve order for an arrow list."""

    out: list[str] = []
    for value in values or []:
        arrow = normalize_arrow(value, default=default)
        if arrow not in out:
            out.append(arrow)
    return out


def normalize_arrows_map(value: Any, default: str = "regulates") -> dict:
    """Normalize a JSONB arrows map without inventing directions."""

    if not isinstance(value, dict):
        return {}
    out = {}
    for key, vals in value.items():
        if isinstance(vals, list):
            normalized = normalize_arrow_list(vals, default=default)
        else:
            normalized = [normalize_arrow(vals, default=default)]
        if normalized:
            out[key] = normalized
    return out


def normalize_chain_arrows(value: Any, default: str = "binds") -> list[dict]:
    """Normalize ``[{from,to,arrow}]`` chain hop payloads."""

    if not isinstance(value, list):
        return []
    out = []
    for hop in value:
        if not isinstance(hop, dict):
            continue
        clean = dict(hop)
        clean["arrow"] = normalize_arrow(clean.get("arrow"), default=default)
        out.append(clean)
    return out


def semantic_claim_direction(value: Any, default: str = "main_to_primary") -> str:
    """Return a claim-level semantic direction.

    Claim directions are rendered from the local pair perspective, so only
    ``main_to_primary`` and ``primary_to_main`` should reach the UI. Absolute
    DB directions are mapped to a stable semantic fallback for legacy rows.
    """

    raw = str(value or "").strip().lower()
    if raw in SEMANTIC_DIRECTION_SET:
        return raw
    if raw == "b_to_a":
        return "primary_to_main"
    return default if default in SEMANTIC_DIRECTION_SET else "main_to_primary"
