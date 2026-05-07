"""Protein symbol normalization + alias resolution.

Two responsibilities:

1. ``normalize_symbol`` — canonical in-memory representation so two
   callers that type the same protein differently produce the same
   lookup key. Handles case, whitespace, Greek letters, zero-width
   characters, and a few punctuation quirks that slip in from LLM
   output. Does NOT strip dashes — some real HGNC symbols contain
   them (``HLA-A``, ``IGF-1R``), so we only collapse "obviously
   decorative" punctuation and leave the rest alone.

2. ``resolve_protein`` — DB-backed alias resolution. Looks the
   normalized symbol up directly first, then falls through to the
   ``ProteinAlias`` table, then falls through to a small hard-coded
   seed map for common synonyms the DB hasn't learned yet. Returns
   the canonical ``Protein`` row (existing or newly created).

The seed map is intentionally tiny — the point isn't to ship a full
HGNC synonym database, it's to close the immediate "Ataxin-3 vs
ATXN3" duplication complaint and leave a well-typed spot for future
entries. Add more rows to ``HARDCODED_ALIAS_SEEDS`` or call
``record_alias()`` from anywhere to grow the map at runtime.
"""
from __future__ import annotations

import re
import sys
import unicodedata
from typing import Optional

from models import Protein, ProteinAlias, db


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

# Greek → Latin letter rewriting. Matches both lowercase and uppercase forms.
# Only the letters that actually show up in protein literature; we're not
# trying to be a full Greek transliterator.
_GREEK_TO_LATIN: dict[str, str] = {
    "α": "a", "Α": "A",
    "β": "b", "Β": "B",
    "γ": "g", "Γ": "G",
    "δ": "d", "Δ": "D",
    "ε": "e", "Ε": "E",
    "ζ": "z", "Ζ": "Z",
    "η": "h", "Η": "H",
    "θ": "th", "Θ": "TH",
    "ι": "i", "Ι": "I",
    "κ": "k", "Κ": "K",
    "λ": "l", "Λ": "L",
    "μ": "u", "Μ": "U",  # micro sign often appears as 'u' or 'mu' in symbols
    "ν": "n", "Ν": "N",
    "ξ": "x", "Ξ": "X",
    "ο": "o", "Ο": "O",
    "π": "p", "Π": "P",
    "ρ": "r", "Ρ": "R",
    "σ": "s", "ς": "s", "Σ": "S",
    "τ": "t", "Τ": "T",
    "υ": "u", "Υ": "U",
    "φ": "f", "Φ": "F",
    "χ": "ch", "Χ": "CH",
    "ψ": "ps", "Ψ": "PS",
    "ω": "o", "Ω": "O",
}

# Zero-width + invisible chars that sometimes hitch a ride through copy-paste.
_ZERO_WIDTH_CHARS = {
    "\u200b", "\u200c", "\u200d", "\ufeff", "\u00ad",
}


def normalize_symbol(raw: Optional[str]) -> str:
    """Return a canonical form of a protein symbol for lookup purposes.

    Rules (in order):
      1. ``None`` / non-str → empty string.
      2. Unicode NFKC normalize (collapses fullwidth / compat forms).
      3. Strip leading/trailing whitespace and a small set of
         "decorative" punctuation (parentheses, brackets).
      4. Remove invisible / zero-width characters.
      5. Greek letters → Latin equivalents.
      6. Collapse internal whitespace to a single underscore? No —
         just strip it; ``IL 2`` → ``IL2``.
      7. Uppercase the whole thing.
      8. Clip to 100 chars (matches the DB column width; avoids row
         rejection on pathological input).

    Note: dashes are NOT stripped. ``HLA-A`` and ``IL-2`` both exist
    as real HGNC symbols (``HLA-A`` really has the dash; ``IL-2`` is
    a common rendering of ``IL2``). The alias table handles the
    dashed variants that aren't canonical on their own.
    """
    if not raw or not isinstance(raw, str):
        return ""
    s = unicodedata.normalize("NFKC", raw)
    for ch in _ZERO_WIDTH_CHARS:
        s = s.replace(ch, "")
    # Drop decorative punctuation that brackets a symbol in prose.
    s = s.strip().strip(".,;:()[]{}\"'")
    # Greek → Latin
    s = "".join(_GREEK_TO_LATIN.get(ch, ch) for ch in s)
    # Collapse internal whitespace (preserves dashes, underscores).
    s = re.sub(r"\s+", "", s)
    return s.upper()[:100]


# ---------------------------------------------------------------------------
# Hard-coded alias seeds (tiny curated list)
# ---------------------------------------------------------------------------

# The point is NOT to ship HGNC in Python. It's to close the obvious
# duplication vector where the same protein arrives under a common
# non-canonical name. Add rows as you encounter them.
#
# Format: canonical HGNC symbol → list of aliases that should resolve
# to it. Aliases are normalized at registration time, so the values
# here can be written in any case with any Greek/whitespace form.
HARDCODED_ALIAS_SEEDS: dict[str, list[str]] = {
    "ATXN3":  ["Ataxin-3", "MJD1", "SCA3", "ATX3", "MJD"],
    "SNCA":   ["α-synuclein", "alpha-synuclein", "aSyn", "a-syn", "NACP", "PARK1", "PARK4"],
    "HTT":    ["Huntingtin", "HD", "IT15"],
    "APP":    ["Amyloid Precursor Protein", "AD1", "CVAP"],
    "MAPT":   ["Tau", "Microtubule Associated Protein Tau", "PPND", "MTBT1"],
    "TARDBP": ["TDP-43", "TDP43"],
    "SOD1":   ["Superoxide Dismutase 1", "ALS1"],
    "PSEN1":  ["Presenilin 1", "AD3"],
    "BCL2":   ["BCL-2", "Bcl2"],
    "TP53":   ["p53", "P53", "TRP53", "LFS1"],
    # Mitophagy / quality-control (common in ATXN3-class chains)
    "PRKN":   ["Parkin", "PARK2"],
    "PINK1":  ["PTEN-induced kinase 1", "PARK6"],
    "SQSTM1": ["p62", "A170"],
    "VCP":    ["p97", "CDC48", "TER ATPase", "VCP/p97"],
    "MFN1":   ["Mitofusin 1", "Mitofusin-1"],
    "MFN2":   ["Mitofusin 2", "Mitofusin-2"],
    "VDAC1":  ["Voltage-Dependent Anion Channel 1", "Porin"],
    # Autophagy
    "MAP1LC3B": ["LC3", "LC3B"],
    "MAP1LC3A": ["LC3A"],
    "GABARAP":  ["GATE-16"],
    # Proteasome shuttles
    "RAD23A": ["HR23A", "hHR23A"],
    "RAD23B": ["HR23B", "hHR23B"],
    # Disease-relevant
    "HSPA8":  ["Hsc70", "HSC70"],
    "DNAJB1": ["Hsp40"],
    "MTOR":   ["mTOR", "FRAP1"],
}


# ---------------------------------------------------------------------------
# DB-backed resolver
# ---------------------------------------------------------------------------

def record_alias(
    canonical_protein: Protein,
    alias: str,
    *,
    source: str = "curated",
) -> Optional[ProteinAlias]:
    """Register ``alias`` as pointing to ``canonical_protein``.

    Idempotent — if an alias row already exists for the normalized
    form, returns the existing row (even if it points at a different
    protein, in which case we refuse to re-map; ambiguous aliases
    stay resolved to whichever Protein claimed them first).

    Returns the ``ProteinAlias`` row on success, or ``None`` if the
    input is invalid (empty alias, missing protein, etc.).
    """
    if not canonical_protein or not canonical_protein.id:
        return None
    norm = normalize_symbol(alias)
    if not norm:
        return None
    # Skip self-aliasing — the canonical symbol is already looked up
    # by ``Protein.symbol`` directly, no alias row needed.
    if norm == (canonical_protein.symbol or "").upper():
        return None
    existing = ProteinAlias.query.filter_by(alias_symbol=norm).first()
    if existing:
        return existing
    row = ProteinAlias(
        alias_symbol=norm,
        protein_id=canonical_protein.id,
        source=source,
    )
    db.session.add(row)
    try:
        db.session.flush()
    except Exception:
        db.session.rollback()
        return None
    return row


def canonicalize_protein_name(raw: Optional[str]) -> str:
    """Return the canonical HGNC symbol for any user-supplied protein name.

    Pipeline at the API edge — every route accepting a ``<protein>`` URL
    parameter or POST body field should call this BEFORE doing any DB
    lookup or starting a job. Without it, ``atxn3`` and ``ATXN3`` hit
    different cache keys and can spawn parallel pipeline runs against
    the same canonical Protein row; aliases like ``MJD`` or ``α-synuclein``
    miss the existing canonical entirely.

    Resolution order:
      1. ``normalize_symbol`` (NFKC, Greek→Latin, uppercase, clip).
      2. Alias table lookup → canonical symbol.
      3. Hard-coded seed map lookup → canonical symbol.
      4. Fall through: return the normalized form as-is.

    Returns the empty string if ``raw`` is empty or non-string.
    Lookups that need to fail closed (no canonical found, no alias)
    should compare ``canonicalize_protein_name(x) ==
    normalize_symbol(x)`` and surface a "not found" hint.
    """
    norm = normalize_symbol(raw)
    if not norm:
        return ""
    try:
        match = lookup_by_alias(norm)
    except Exception:
        # Any DB hiccup at the alias layer falls through to the
        # normalized form — don't block the route over an alias miss.
        match = None
    if match and match.symbol:
        return match.symbol
    return norm


def lookup_by_alias(raw_symbol: str) -> Optional[Protein]:
    """Return the canonical ``Protein`` row for any alias form.

    Order:
      1. Direct lookup by normalized symbol (covers canonical symbols).
      2. Alias table lookup.
      3. Hard-coded seed map lookup (returns None if the canonical
         doesn't exist yet; caller decides whether to create it).

    Returns ``None`` when no match is found at any tier. Never
    creates a row — that's the caller's decision.
    """
    norm = normalize_symbol(raw_symbol)
    if not norm:
        return None

    # Tier 1 — canonical symbol lookup.
    protein = Protein.query.filter_by(symbol=norm).first()
    if protein:
        return protein

    # Tier 2 — alias table.
    alias_row = ProteinAlias.query.filter_by(alias_symbol=norm).first()
    if alias_row:
        # Load the canonical protein via the FK. If the target row
        # was hard-deleted without cascading (shouldn't happen —
        # ondelete='CASCADE' — but defensive), return None.
        return db.session.get(Protein, alias_row.protein_id)

    # Tier 3 — hard-coded seed map. Translate the alias to the
    # canonical symbol and look THAT up. If the canonical isn't in
    # the DB yet, we don't create it here; _get_or_create_protein
    # handles the create+alias-registration flow when creating a
    # new canonical row.
    for canonical_sym, seed_aliases in HARDCODED_ALIAS_SEEDS.items():
        for seed in seed_aliases:
            if normalize_symbol(seed) == norm:
                return Protein.query.filter_by(symbol=canonical_sym).first()

    return None


def ensure_seed_aliases_registered(protein: Protein) -> int:
    """Register any hard-coded seed aliases for ``protein`` in the DB.

    Called by ``_get_or_create_protein`` when a new canonical row is
    created. Returns the number of alias rows successfully added.
    Idempotent — repeat invocations are harmless.
    """
    if not protein or not protein.symbol:
        return 0
    seeds = HARDCODED_ALIAS_SEEDS.get(protein.symbol.upper())
    if not seeds:
        return 0
    added = 0
    for seed in seeds:
        if record_alias(protein, seed, source="HGNC_SEED") is not None:
            added += 1
    return added


def bulk_seed_hardcoded_aliases(verbose: bool = False) -> dict[str, int]:
    """One-shot populate ``ProteinAlias`` from ``HARDCODED_ALIAS_SEEDS``.

    Intended to be called from a CLI or startup hook. Skips canonical
    proteins that don't exist yet (we don't create them here — they
    appear naturally as queries come in, and the alias table picks
    them up via ``ensure_seed_aliases_registered`` at creation time).

    Returns per-category stats so the caller can decide whether to
    log / report.
    """
    stats = {"added": 0, "skipped_missing_canonical": 0, "existing": 0}
    for canonical_sym, seed_aliases in HARDCODED_ALIAS_SEEDS.items():
        canonical = Protein.query.filter_by(symbol=canonical_sym).first()
        if not canonical:
            stats["skipped_missing_canonical"] += 1
            if verbose:
                print(f"[ALIAS SEED] {canonical_sym}: canonical not in DB yet", file=sys.stderr)
            continue
        for seed in seed_aliases:
            norm = normalize_symbol(seed)
            if not norm or norm == canonical_sym:
                continue
            if ProteinAlias.query.filter_by(alias_symbol=norm).first():
                stats["existing"] += 1
                continue
            if record_alias(canonical, seed, source="HGNC_SEED"):
                stats["added"] += 1
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
    return stats
