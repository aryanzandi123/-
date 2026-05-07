"""Canonical helpers for translating between the two direction terminologies
used across the codebase.

The codebase has two parallel direction vocabularies:

1. **Semantic** (used in pipeline prompts and LLM output):
   - ``main_to_primary`` — query protein acts on partner protein
   - ``primary_to_main`` — partner protein acts on query protein

2. **Canonical storage** (used in the PostgreSQL ``interactions`` table
   because the pair is stored with ``protein_a_id < protein_b_id``):
   - ``a_to_b`` — protein_a acts on protein_b
   - ``b_to_a`` — protein_b acts on protein_a

S1: ``bidirectional`` is DEAD. Every interaction has an asymmetric
direction. Symmetric arrows (binds, complex, interacts) default to
``main_to_primary`` — the query protein is always framed as the active
participant in the pipeline's query-centric model. Historical rows
with ``direction='bidirectional'`` are migrated to ``a_to_b`` via
``scripts/migrate_kill_bidirectional.py``.

Translation depends on whether the query protein is protein_a or protein_b
in the canonical ordering. These helpers centralize that translation so
callers don't have to re-derive the logic each time.
"""
from __future__ import annotations

from typing import Literal, Optional

SemanticDirection = Literal["main_to_primary", "primary_to_main"]
CanonicalDirection = Literal["a_to_b", "b_to_a"]

_VALID_SEMANTIC = {"main_to_primary", "primary_to_main"}
_VALID_CANONICAL = {"a_to_b", "b_to_a"}

# S1: legacy value — kept as a constant for migration/cleanup code that
# needs to detect and replace it, not for production use.
_LEGACY_BIDIRECTIONAL = "bidirectional"


def semantic_to_canonical(
    direction: Optional[str],
    query_is_protein_a: bool,
) -> Optional[str]:
    """Translate semantic ``main_to_primary``/``primary_to_main`` to canonical
    ``a_to_b``/``b_to_a`` based on which side the query protein sits on.

    S1: ``bidirectional`` is treated as ``main_to_primary`` (the default
    for symmetric arrows) and converted to the appropriate canonical form.
    """
    if direction is None:
        return None
    # S1: treat legacy bidirectional as main_to_primary
    if direction == _LEGACY_BIDIRECTIONAL:
        direction = "main_to_primary"
    if direction not in _VALID_SEMANTIC:
        return direction if direction in _VALID_CANONICAL else None
    if query_is_protein_a:
        return "a_to_b" if direction == "main_to_primary" else "b_to_a"
    return "b_to_a" if direction == "main_to_primary" else "a_to_b"


def canonical_to_semantic(
    direction: Optional[str],
    query_is_protein_a: bool,
) -> Optional[str]:
    """Inverse of :func:`semantic_to_canonical`.

    S1: ``bidirectional`` is converted to ``main_to_primary``.
    """
    if direction is None:
        return None
    # S1: treat legacy bidirectional as main_to_primary
    if direction == _LEGACY_BIDIRECTIONAL:
        return "main_to_primary"
    if direction not in _VALID_CANONICAL:
        return direction if direction in _VALID_SEMANTIC else None
    if query_is_protein_a:
        return "main_to_primary" if direction == "a_to_b" else "primary_to_main"
    return "primary_to_main" if direction == "a_to_b" else "main_to_primary"


def normalize_to_canonical(direction: Optional[str], query_is_protein_a: bool) -> Optional[str]:
    """Accept either flavor and return the canonical form (used at write boundaries)."""
    if direction in _VALID_CANONICAL:
        return direction
    return semantic_to_canonical(direction, query_is_protein_a)


def normalize_to_semantic(direction: Optional[str], query_is_protein_a: bool) -> Optional[str]:
    """Accept either flavor and return the semantic form (used at read boundaries)."""
    if direction in _VALID_SEMANTIC:
        return direction
    return canonical_to_semantic(direction, query_is_protein_a)


# ---------------------------------------------------------------------------
# Arrow → direction inference
# ---------------------------------------------------------------------------

# Arrows implying the query protein acts ON the partner (subject→object).
_ARROWS_QUERY_ACTS = frozenset({
    "activates", "inhibits", "regulates", "phosphorylates",
    "dephosphorylates", "ubiquitinates", "deubiquitinates",
    "methylates", "demethylates", "acetylates", "deacetylates",
    "cleaves", "degrades", "stabilizes", "destabilizes",
    "represses", "induces", "suppresses", "promotes",
    "recruits", "sequesters", "transports", "translocates",
    "modifies",
})

# Arrows implying the partner protein acts ON the query (object←subject).
_ARROWS_PARTNER_ACTS = frozenset({
    "is_substrate_of", "is_activated_by", "is_inhibited_by",
    "is_phosphorylated_by", "is_ubiquitinated_by",
    "is_degraded_by", "is_cleaved_by", "is_regulated_by",
})

# Symmetric arrows — no inherent direction. Convention: default to
# ``main_to_primary`` (the query protein is the canonical subject in
# the pipeline's discovery framing). S1: these used to return
# ``bidirectional`` which caused direction drift everywhere.
_SYMMETRIC_ARROWS = frozenset({
    "binds", "complex", "interacts", "associates",
    "co-localizes", "colocalizes",
})


def infer_direction_from_arrow(arrow: Optional[str]) -> str:
    """Return a best-guess semantic direction for an interaction arrow.

    Differentiates by arrow semantics:
      - Agent verbs (activates, inhibits, …) → ``main_to_primary``
      - Passive verbs (is_substrate_of, …)   → ``primary_to_main``
      - Symmetric verbs (binds, complex, …)  → ``main_to_primary``
        (convention: query is canonical subject)
      - Unknown / None                        → ``main_to_primary``

    S1: NEVER returns ``bidirectional``. Every interaction gets a real
    asymmetric direction.
    """
    if not arrow or not isinstance(arrow, str):
        return "main_to_primary"
    normalized = arrow.strip().lower()
    if not normalized:
        return "main_to_primary"
    if normalized in _ARROWS_PARTNER_ACTS:
        return "primary_to_main"
    if normalized in _ARROWS_QUERY_ACTS:
        return "main_to_primary"
    if normalized in _SYMMETRIC_ARROWS:
        return "main_to_primary"
    return "main_to_primary"


def is_more_specific_direction(new_dir: Optional[str], existing_dir: Optional[str]) -> bool:
    """Return True when ``new_dir`` is strictly more specific than ``existing_dir``.

    S1: ``bidirectional`` is treated the same as ``None`` (unset) for the
    existing direction — a legacy row with ``bidirectional`` can be
    overwritten by any real asymmetric direction.
    """
    if not new_dir or new_dir == _LEGACY_BIDIRECTIONAL:
        return False
    if existing_dir is None or existing_dir == _LEGACY_BIDIRECTIONAL:
        return True
    return False
