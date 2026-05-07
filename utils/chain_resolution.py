"""Pure-code utilities for chain resolution pipeline (steps 2ab2, 2ab3, 2ab4, 2ab5)."""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

__all__ = [
    "extract_candidate_proteins",
    "extract_candidates_from_payload",
    "extract_new_interaction_pairs",
    "extract_new_pairs_with_existing_check",
    "build_chain_from_indirect",
    "deduplicate_chain_claims",
    "assign_shared_pathway",
    "format_chain_notation",
    "canonical_pair_key",
    "canonicalize_chain_direction",
    "canonicalize_chain_link_functions",
    "validate_chain_on_ingest",
    "is_valid_chain_protein_symbol",
]


# ---------------------------------------------------------------------------
# Canonical chain-link-function keys
# ---------------------------------------------------------------------------
#
# Historically, ``chain_link_functions`` was a dict keyed by directional
# strings like ``"ATXN3->VCP"``. That encoding was ambiguous: the same
# biological pair could end up under ``"A->B"`` or ``"B->A"`` depending on
# which direction the LLM chose to describe, and downstream lookups missed
# one orientation or the other. The "fix" was a reverse-direction fallback,
# which just pushed the bug one step further.
#
# The root fix is to have ONE canonical key per protein pair, independent
# of direction. Every writer canonicalizes on ingest; every reader
# canonicalizes its lookup; direction lives in the per-function ``arrow``
# field, not in the dict key.
#
# Canonical form: ``"A|B"`` where A and B are the two symbols sorted
# alphabetically (case-insensitively). Alphabetical sort is stable and
# works for any pair of symbols; the ``|`` separator is unambiguous
# because HGNC symbols never contain a pipe.

_PAIR_SEPARATOR = "|"


def canonical_pair_key(a: str, b: str) -> str:
    """Return the canonical (direction-agnostic) key for a protein pair.

    ``canonical_pair_key("VCP", "ATXN3")`` → ``"ATXN3|VCP"``
    ``canonical_pair_key("ATXN3", "VCP")`` → ``"ATXN3|VCP"``
    ``canonical_pair_key("vcp", "Atxn3")`` → ``"ATXN3|VCP"`` (case-insensitive)

    Returned key is UPPERCASED. Previously the function sorted by
    case-insensitive comparison but emitted whichever casing it
    received first, so ``canonical_pair_key("VCP","TFEB")`` and
    ``canonical_pair_key("vcp","tfeb")`` produced two DIFFERENT keys
    (``"TFEB|VCP"`` vs ``"tfeb|vcp"``), causing
    ``_attach_chain_claim_records`` to silently miss matches. Concrete
    symptom: ``[PARALLEL:az_claim_generation_hidden_depth_expand]
    Attached chain claims for 0/1 requested pair(s)`` for every depth-
    expanded hidden hop. Uppercasing both inputs before sorting makes
    the key truly canonical regardless of writer-vs-reader casing.

    Empty / falsy symbols preserve their position (so the caller can
    detect malformed input instead of having empty keys collide).
    """
    if not a or not b:
        return f"{a or ''}{_PAIR_SEPARATOR}{b or ''}"
    lo, hi = sorted([str(a).strip().upper(), str(b).strip().upper()])
    return f"{lo}{_PAIR_SEPARATOR}{hi}"


# ---------------------------------------------------------------------------
# Canonical chain direction (Layer 1 of CLAUDE_DOCS/11_CHAIN_TOPOLOGY.md)
# ---------------------------------------------------------------------------
#
# The LLM emits chains in query-centric order: for an ATXN3 query whose
# cascade is biologically STUB1 → HSP90AA1 → (effect on ATXN3), it may
# write ``chain_proteins = ["ATXN3", "HSP90AA1", "STUB1"]`` because that's
# how it was framing the cascade. The frontend tree layout renders
# ``chain[k+1]`` as a child of ``chain[k]`` — so STUB1 lands visually
# downstream of HSP90AA1 even though biologically it's upstream.
#
# Fix: at write time, when the chain's arrows are dominated by reverse-
# direction verbs (``is_substrate_of``, ``is_phosphorylated_by``, …),
# reverse BOTH the protein order AND the arrow list in lockstep so
# ``chain[i] → chain[i+1]`` is always biological cause → effect.
#
# Mixed-direction chains (forward ≈ reverse counts) keep the LLM order;
# the per-edge arrow labels carry the biological direction in those cases.

_REVERSE_VERBS = frozenset({
    "is_substrate_of",
    "is_activated_by",
    "is_inhibited_by",
    "is_phosphorylated_by",
    "is_ubiquitinated_by",
    "is_degraded_by",
    "is_cleaved_by",
    "is_regulated_by",
    "is_recruited_by",
    "is_stabilized_by",
    "is_destabilized_by",
    "is_repressed_by",
    "is_induced_by",
    "is_sequestered_by",
    "is_bound_by",
})

_FORWARD_VERBS = frozenset({
    "activates",
    "inhibits",
    "phosphorylates",
    "ubiquitinates",
    "cleaves",
    "degrades",
    "stabilizes",
    "destabilizes",
    "represses",
    "induces",
    "recruits",
    "sequesters",
    "binds",
    "regulates",
    "deubiquitinates",
    "dephosphorylates",
})


def canonicalize_chain_direction(
    chain_proteins: Optional[List[str]],
    chain_with_arrows: Optional[List[Dict[str, Any]]],
) -> Tuple[List[str], List[Dict[str, Any]], bool]:
    """Return ``(proteins, arrows, was_reversed)`` ordered cause → effect.

    Reverses both arrays in lockstep when reverse-direction verbs strictly
    dominate the chain's arrow set. Mixed-direction chains (forward ≈
    reverse) keep their original LLM order — direction is then conveyed
    by per-edge arrow labels rendered by the frontend.

    Empty / single-element chains are returned as-is with
    ``was_reversed=False``.
    """
    proteins_out = list(chain_proteins or [])
    arrows_out = list(chain_with_arrows or [])
    if not arrows_out or len(proteins_out) < 2:
        return proteins_out, arrows_out, False

    fwd = 0
    rev = 0
    for entry in arrows_out:
        if not isinstance(entry, dict):
            continue
        verb = str(entry.get("arrow", "")).strip().lower()
        if verb in _FORWARD_VERBS:
            fwd += 1
        elif verb in _REVERSE_VERBS:
            rev += 1

    if rev > fwd:
        return list(reversed(proteins_out)), list(reversed(arrows_out)), True
    return proteins_out, arrows_out, False


def _parse_directional_key(key: str) -> Optional[Tuple[str, str]]:
    """Parse a legacy directional key ``"A->B"`` or canonical ``"A|B"``.

    Returns ``(a, b)`` or ``None`` if the key doesn't match either shape.
    """
    if not isinstance(key, str) or not key:
        return None
    if "->" in key:
        parts = [p.strip() for p in key.split("->", 1)]
        if len(parts) == 2 and parts[0] and parts[1]:
            return parts[0], parts[1]
    if _PAIR_SEPARATOR in key:
        parts = [p.strip() for p in key.split(_PAIR_SEPARATOR, 1)]
        if len(parts) == 2 and parts[0] and parts[1]:
            return parts[0], parts[1]
    return None


def canonicalize_chain_link_functions(
    clf: Optional[Dict[str, Any]],
) -> Dict[str, List[Dict[str, Any]]]:
    """Rewrite a ``chain_link_functions`` dict so every key is canonical.

    Accepts legacy directional keys (``"A->B"``) and pre-canonicalized
    keys (``"A|B"``) in the same dict, and merges function lists for pairs
    that end up under the same canonical key. Functions are deduplicated
    by ``(function_name.lower())`` within each pair.

    Returns a new dict; does not mutate the input.
    """
    if not clf or not isinstance(clf, dict):
        return {}

    out: Dict[str, List[Dict[str, Any]]] = {}
    for raw_key, funcs in clf.items():
        pair = _parse_directional_key(raw_key)
        if pair is None:
            # Unparseable key — keep as-is under its own canonical slot so
            # we don't silently drop data. Downstream code will ignore it.
            out.setdefault(str(raw_key), []).extend(
                f for f in (funcs or []) if isinstance(f, dict)
            )
            continue
        a, b = pair
        key = canonical_pair_key(a, b)
        existing = out.setdefault(key, [])
        seen = {(f.get("function") or "").strip().lower() for f in existing if isinstance(f, dict)}
        for f in (funcs or []):
            if not isinstance(f, dict):
                continue
            fn_name = (f.get("function") or "").strip().lower()
            if fn_name and fn_name in seen:
                continue
            existing.append(f)
            if fn_name:
                seen.add(fn_name)
    return out


# ---------------------------------------------------------------------------
# Chain ingest validator
# ---------------------------------------------------------------------------


def validate_chain_on_ingest(
    chain: List[str],
    query_protein: Optional[str] = None,
) -> Tuple[List[str], List[str]]:
    """Validate and clean an LLM-returned chain before it reaches the DB.

    Applies the following rules in order:

      1. Strip marker characters (``^``, ``*``) and whitespace from each
         entry; drop empty entries.
      2. Reject non-string entries.
      3. Deduplicate consecutive repeats (A→A→B becomes A→B), preserving
         the first occurrence.
      4. If ``query_protein`` is given and it's NOT in the cleaned chain,
         report an error (but don't reject — the caller decides what to do).
      5. Reject chains containing generic non-protein entities (RNA,
         ubiquitin, proteasome, processes, compartments). Those belong in
         claim text, not as graph nodes or DB protein rows.

    Returns ``(cleaned_chain, errors)``. ``errors`` is a list of
    human-readable messages for any rule that fired; empty means the
    chain was well-formed.

    Pure function — no I/O, safe to call from anywhere.
    """
    errors: List[str] = []

    if not isinstance(chain, list):
        errors.append(f"chain is not a list (got {type(chain).__name__})")
        return [], errors

    # 1–2. Clean entries.
    cleaned: List[str] = []
    for idx, raw in enumerate(chain):
        if not isinstance(raw, str):
            errors.append(f"chain[{idx}] is not a string (got {type(raw).__name__})")
            continue
        sym = raw.strip().strip("^*").strip()
        if not sym:
            continue
        cleaned.append(sym)

    # 3. Drop consecutive duplicates (A→A→B → A→B).
    deduped: List[str] = []
    for sym in cleaned:
        if deduped and deduped[-1].upper() == sym.upper():
            continue
        deduped.append(sym)

    invalid_symbols = [sym for sym in deduped if not is_valid_chain_protein_symbol(sym)]
    if invalid_symbols:
        errors.append(
            "chain contains non-protein/generic entity nodes: "
            + ", ".join(invalid_symbols)
        )
        return [], errors

    # Non-adjacent repetition (``A → B → C → A``) is ALLOWED. Per user
    # principle: a protein can appear at multiple positions in a cascade
    # when each position represents a distinct biological role — e.g. in
    # TDP43→RANGAP1→KPNA2→RANGAP1, the first RANGAP1 is the TDP43-bound
    # nuclear-pore role and the second is the KPNA2-returned nuclear-
    # import role. Each hop is its own sci-claim, its own modal. We log
    # the repetition once for observability but do NOT truncate.
    seen_positions: Dict[str, List[int]] = {}
    for idx, sym in enumerate(deduped):
        seen_positions.setdefault(sym.upper(), []).append(idx)
    for sym_u, positions in seen_positions.items():
        if len(positions) > 1:
            errors.append(
                f"chain revisits {sym_u} at positions {positions} — "
                "treating each occurrence as a distinct hop (not truncating)"
            )

    # 4. Confirm query is present if we care.
    if query_protein:
        qp_upper = query_protein.upper()
        if qp_upper not in (p.upper() for p in deduped):
            errors.append(
                f"chain does not contain query protein {query_protein!r}"
            )

    return deduped, errors

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_NON_PROTEIN_TERMS: set[str] = {
    # Nucleic acids & related
    "DNA", "RNA", "mRNA", "tRNA", "rRNA", "siRNA", "miRNA", "shRNA",
    "lncRNA", "cDNA", "ssDNA", "dsDNA", "gDNA",
    # Generic macromolecules/complexes that are not specific HGNC symbols
    "Ubiquitin", "Polyubiquitin", "Proteasome", "Ribosome", "Actin",
    "Tubulin", "Microtubule", "Chromatin", "Histone",
    # Nucleotides & energy carriers
    "ATP", "ADP", "AMP", "GTP", "GDP", "GMP", "CTP", "UTP", "NAD",
    "NADH", "NADP", "NADPH", "FAD", "FADH", "CoA",
    # Common biology abbreviations
    "ER", "ERAD", "UPS", "UPR", "ISR", "NMD", "IRES", "UTR",
    "ORF", "CDS", "SNP", "CNV", "LOH", "LOF", "GOF",
    # Cellular structures & processes
    "ROS", "RNS", "NO", "pH", "Ca", "Zn", "Fe", "Mg", "Mn", "Cu",
    "PI", "PIP", "DAG", "IP3", "cAMP", "cGMP",
    # Experimental / technical terms
    "PCR", "qPCR", "FACS", "ELISA", "EMSA", "ChIP", "CLIP",
    "CRISPR", "Cas9", "Cas12", "TALENs", "ZFN",
    "SDS", "PAGE", "PVDF", "HRP", "GFP", "YFP", "CFP", "RFP", "BFP",
    "FLAG", "HA", "MYC", "GST", "HIS",
    "Western", "Northern", "Southern", "Eastern",
    "ANOVA", "DMSO", "EDTA", "HEPES", "PBS", "BSA", "FBS",
    # Cell lines & model organisms
    "HeLa", "HEK", "CHO", "COS", "NIH", "MEF", "PBMC",
    "WT", "KO", "KD", "OE", "het",
    # General English / biology terms that look like protein names
    "THE", "AND", "FOR", "NOT", "WITH", "FROM", "INTO", "OVER",
    "CELL", "GENE", "TYPE", "SITE", "ROLE", "LOSS", "GAIN",
    "HIGH", "LOW", "BOTH", "EACH", "ALSO", "MANY", "MOST",
    "DOMAIN", "MOTIF", "LOOP", "SIGNAL", "PATHWAY",
    "RECEPTOR", "LIGAND", "KINASE", "PHOSPHATASE", "PROTEASE",
    "ENZYME", "SUBSTRATE", "COMPLEX", "FACTOR", "CHAIN",
    "ALPHA", "BETA", "GAMMA", "DELTA", "KAPPA", "SIGMA",
    "FAMILY", "CLASS", "GROUP", "ISOFORM", "VARIANT",
    "CANCER", "TUMOR", "NORMAL", "TISSUE", "PLASMA",
    "MEMBRANE", "CYTOPLASM", "NUCLEUS", "LYSOSOME", "GOLGI",
    "APOPTOSIS", "AUTOPHAGY", "NECROSIS", "FERROPTOSIS",
    "MITOSIS", "MEIOSIS", "TRANSLATION", "TRANSCRIPTION",
    "SPLICING", "METHYLATION", "ACETYLATION", "UBIQUITINATION",
    "SUMOYLATION", "PHOSPHORYLATION", "GLYCOSYLATION",
    "DEATH", "GROWTH", "CYCLE", "REPAIR", "DAMAGE",
    "STRESS", "RESPONSE", "ACTIVATION", "INHIBITION",
    "EXPRESSION", "REGULATION", "DEGRADATION", "ASSEMBLY",
    "BINDING", "INTERACTION", "SIGNALING", "TRANSPORT",
    "SECRETION", "ENDOCYTOSIS", "EXOCYTOSIS",
}

# Pre-compile the lookup as a frozenset of lowercased terms for fast matching.
_NON_PROTEIN_LOWER: frozenset[str] = frozenset(t.lower() for t in _NON_PROTEIN_TERMS)

# Regex: likely protein/gene token — uppercase 2-15 chars, may contain digits/hyphens.
_PROTEIN_RE = re.compile(
    r"""
    \b
    (
        [A-Z][A-Z0-9]{1,14}            # e.g. VCP, ATXN3, RAD23A
        (?:-[A-Z0-9]{1,6})?            # optional hyphen suffix, e.g. HIF-1A
    |
        [A-Z][a-z]{2,}[0-9]+           # e.g. Ufd1, Npl4
    |
        [A-Z][a-z]+[A-Z][a-zA-Z0-9]*   # camelCase proteins, e.g. mTOR
    |
        [Cc][0-9]+[Oo][Rr][Ff][0-9]+   # HGNC Corf naming: C9orf72, C19orf12,
                                        # C1orf123. Common in ALS/neuro
                                        # literature; was being mis-classified
                                        # as non-protein by the chain validator.
    )
    \b
    """,
    re.VERBOSE,
)


def is_valid_chain_protein_symbol(symbol: str) -> bool:
    """Return True only for specific protein/gene symbols usable as nodes."""
    if not isinstance(symbol, str):
        return False
    clean = symbol.strip().strip("^*").strip()
    if not clean:
        return False
    lower = clean.lower()
    if lower in _NON_PROTEIN_LOWER:
        return False
    if lower.endswith(("mrna", "pre-mrna", "rna")):
        return False
    if " " in clean:
        return False
    return bool(_PROTEIN_RE.fullmatch(clean))


# ---------------------------------------------------------------------------
# 1. extract_candidate_proteins  (step 2ab2 code)
# ---------------------------------------------------------------------------

def _collect_text_fields(claim: Dict[str, Any]) -> str:
    """Concatenate all text-bearing fields of a claim into one string."""
    parts: list[str] = []
    for key in ("cellular_process", "effect_description"):
        val = claim.get(key)
        if isinstance(val, str):
            parts.append(val)
    for key in ("biological_consequence", "specific_effects"):
        val = claim.get(key)
        if isinstance(val, list):
            for item in val:
                if isinstance(item, str):
                    parts.append(item)
    return " ".join(parts)


def extract_candidate_proteins(
    claim: Dict[str, Any],
    query: str,
    interactor: str,
) -> list[str]:
    """Extract protein/gene names from claim text that are not query or interactor."""
    text = _collect_text_fields(claim)
    if not text:
        return []

    query_lower = query.lower()
    interactor_lower = interactor.lower()

    seen: set[str] = set()
    result: list[str] = []

    for match in _PROTEIN_RE.finditer(text):
        token = match.group(0)
        token_lower = token.lower()

        # Skip the query and interactor themselves.
        if token_lower == query_lower or token_lower == interactor_lower:
            continue

        # Skip tokens that CONTAIN the query or interactor (e.g. "VCP-ATXN3").
        if query_lower in token_lower or interactor_lower in token_lower:
            continue

        # Skip known non-protein/generic terms.
        if not is_valid_chain_protein_symbol(token):
            continue

        # Skip very short all-uppercase tokens that are likely abbreviations.
        if len(token) <= 2 and token.isupper():
            continue

        # Skip 3-letter tokens that are common motif/domain abbreviations.
        if len(token) == 3 and token.isupper() and token not in (
            "AKT", "BAX", "BAK", "BCL", "BID", "BIM", "ERK", "JNK", "JUN",
            "FOS", "MYC", "RAS", "RAF", "SRC", "ABL", "JAK", "MDM", "P53",
            "RB1", "MAX", "MET", "KIT", "ALK", "RET", "FYN", "LCK", "SYK",
        ):
            continue

        if token_lower not in seen:
            seen.add(token_lower)
            result.append(token)

    return result


# ---------------------------------------------------------------------------
# 2. extract_candidates_from_payload  (step 2ab2 code)
# ---------------------------------------------------------------------------

def extract_candidates_from_payload(payload: Dict[str, Any]) -> list[Dict[str, Any]]:
    """Scan direct interaction claims and return candidate hidden-indirect proteins."""
    ctx = payload.get("ctx_json", {})
    query = ctx.get("main", "")
    interactors = ctx.get("interactors", [])
    results: list[Dict[str, Any]] = []

    for interactor_obj in interactors:
        itype = (interactor_obj.get("interaction_type") or "direct").lower()
        if itype != "direct":
            continue

        primary = interactor_obj.get("primary", "")
        functions = interactor_obj.get("functions", []) or []

        for idx, claim in enumerate(functions):
            if not isinstance(claim, dict):
                continue

            candidates = extract_candidate_proteins(claim, query, primary)
            if not candidates:
                continue

            text = _collect_text_fields(claim)
            snippet = text[:200] if text else ""

            results.append({
                "interactor": primary,
                "claim_index": idx,
                "claim_function_name": claim.get("function", ""),
                "candidate_proteins": candidates,
                "source_text_snippet": snippet,
            })

    return results


# ---------------------------------------------------------------------------
# 3. extract_new_interaction_pairs  (step 2ab4 pure code)
# ---------------------------------------------------------------------------

def extract_new_interaction_pairs(
    chain: list[str],
    original_pair: Tuple[str, str],
) -> Dict[str, Any]:
    """Extract new direct and indirect pairs from a resolved chain.

    Processes ALL new proteins in the chain (not just the closest),
    generating pairs between every consecutive member.
    Returns new_protein (closest, for backward compat) plus all pairs.
    """
    if len(chain) < 2:
        return {"new_protein": None, "new_directs": [], "new_indirects": []}

    orig_set_lower = {p.lower() for p in original_pair}

    # Find new proteins (not in original pair).
    new_proteins = [p for p in chain if p.lower() not in orig_set_lower]
    if not new_proteins:
        return {"new_protein": None, "new_directs": [], "new_indirects": []}

    chain_lower = [p.lower() for p in chain]
    orig_indices = [i for i, cl in enumerate(chain_lower) if cl in orig_set_lower]
    if not orig_indices:
        return {"new_protein": None, "new_directs": [], "new_indirects": []}

    # Pick the closest new protein for backward compatibility (returned as new_protein)
    best_protein: str | None = None
    best_distance = len(chain) + 1
    for np_name in new_proteins:
        np_idx = chain_lower.index(np_name.lower())
        min_dist = min(abs(np_idx - oi) for oi in orig_indices)
        if min_dist < best_distance:
            best_distance = min_dist
            best_protein = np_name

    if best_protein is None:
        return {"new_protein": None, "new_directs": [], "new_indirects": []}

    # Generate ALL consecutive pairs in the chain (not just for closest protein)
    new_directs: list[Dict[str, Any]] = []
    new_indirects: list[Dict[str, Any]] = []
    seen_pairs: set = set()

    for i in range(len(chain) - 1):
        src = chain[i]
        tgt = chain[i + 1]
        pair_key = (src.lower(), tgt.lower())
        if pair_key in seen_pairs:
            continue
        seen_pairs.add(pair_key)

        # Skip pairs where both are in original pair (already known)
        if src.lower() in orig_set_lower and tgt.lower() in orig_set_lower:
            continue

        new_directs.append({
            "pair": (src, tgt),
            "direction": "upstream_to_downstream",
        })

    # Also generate non-adjacent pairs (indirect through mediators)
    for np_name in new_proteins:
        np_idx = chain_lower.index(np_name.lower())
        for oi in orig_indices:
            pair_key = tuple(sorted([chain[np_idx].lower(), chain[oi].lower()]))
            if pair_key in seen_pairs:
                continue
            seen_pairs.add(pair_key)
            if abs(np_idx - oi) > 1:
                lo, hi = sorted([oi, np_idx])
                sub_chain = chain[lo : hi + 1]
                if oi < np_idx:
                    pair = (chain[oi], np_name)
                else:
                    pair = (np_name, chain[oi])
                new_indirects.append({
                    "pair": pair,
                    "direction": "upstream_to_downstream",
                    "chain": sub_chain,
                })

    return {
        "new_protein": best_protein,
        "new_directs": new_directs,
        "new_indirects": new_indirects,
    }


# ---------------------------------------------------------------------------
# 4. extract_new_pairs_with_existing_check  (step 2ab5)
# ---------------------------------------------------------------------------

def extract_new_pairs_with_existing_check(
    chain: list[str],
    original_pair: Tuple[str, str],
    existing_interactors: list[Dict[str, Any]],
) -> Dict[str, Any]:
    """Like extract_new_interaction_pairs but checks for pre-existing interactors."""
    base = extract_new_interaction_pairs(chain, original_pair)
    new_protein = base.get("new_protein")

    if not new_protein:
        return {**base, "new_protein_already_exists": False, "existing_claims_for_comparison": []}

    # Check if new_protein already exists as a direct interactor of the query.
    existing_claims: list[Dict[str, Any]] = []
    already_exists = False
    new_lower = new_protein.lower()

    for inter in existing_interactors:
        primary = (inter.get("primary") or "").lower()
        if primary == new_lower:
            itype = (inter.get("interaction_type") or "direct").lower()
            if itype == "direct":
                already_exists = True
                existing_claims = inter.get("functions", []) or []
            break

    return {
        **base,
        "new_protein_already_exists": already_exists,
        "existing_claims_for_comparison": existing_claims,
    }


# ---------------------------------------------------------------------------
# 5. build_chain_from_indirect  (helper)
# ---------------------------------------------------------------------------

def build_chain_from_indirect(interactor: Dict[str, Any], query: str) -> list[str]:
    """Build the full ordered chain list from an indirect interactor record.

    Prefers ``chain_context.full_chain`` (canonical, preserves the query's
    biological position — head, middle, or tail). Falls back to the
    minimal ``[query, primary]`` pair when no canonical chain is
    available: the old ``[query] + mediator_chain + [primary]``
    reconstruction force-prepended the query and silently inverted
    query-at-tail chains, which is exactly the bug that caused the
    ``[CHAIN HOP CLAIM MISSING]`` floods. Callers that need a real
    multi-hop chain must populate ``chain_context.full_chain`` upstream.
    """
    from utils.chain_view import ChainView

    primary = interactor.get("primary", "")

    view = ChainView.from_interaction_data(interactor, query_protein=query)
    if not view.is_empty:
        return list(view.full_chain)

    # No canonical chain available — minimal pair. Never reconstruct
    # with a query-at-head assumption (that was the direction bug).
    return [query, primary]


# ---------------------------------------------------------------------------
# 6. deduplicate_chain_claims  (steps 2ax/2az)
# ---------------------------------------------------------------------------

def _significant_words(text: str) -> set[str]:
    """Extract lowercased words of length >= 3 from *text*."""
    if not text:
        return set()
    return {w.lower() for w in re.findall(r"[A-Za-z0-9]{3,}", text) if w.lower() not in _NON_PROTEIN_LOWER}


def _word_overlap_ratio(a: set[str], b: set[str]) -> float:
    """Return Jaccard-like overlap: |intersection| / |smaller set|."""
    if not a or not b:
        return 0.0
    intersection = a & b
    smaller = min(len(a), len(b))
    return len(intersection) / smaller


def _acronym_matches_phrase(acronym: str, words: list[str]) -> bool:
    """Check if *acronym* can be formed by taking leading chars from *words* in order.

    Handles biology acronyms where some words contribute multiple leading
    characters (e.g. "ER-associated degradation" -> "ERAD": ER=2 chars, A=1, D=1).
    """
    if not acronym or not words:
        return False

    # Recursive helper with memoisation.
    def _match(ai: int, wi: int, ci: int) -> bool:
        """Can acronym[ai:] be matched starting at words[wi][ci:]?"""
        if ai == len(acronym):
            return True
        if wi >= len(words):
            return False

        word = words[wi]
        # Try consuming 1..N leading characters from current word.
        for take in range(1, len(word) - ci + 1):
            if word[ci : ci + take] == acronym[ai : ai + take]:
                # Continue within same word (more chars) or move to next word.
                if _match(ai + take, wi + 1, 0):
                    return True
            else:
                break
        # Skip current word entirely (optional word in expansion).
        return _match(ai, wi + 1, 0)

    return _match(0, 0, 0)


def _fuzzy_name_match(name_a: str, name_b: str) -> bool:
    """Return True if two function names are similar enough to be considered the same."""
    a = name_a.strip().lower()
    b = name_b.strip().lower()
    if a == b:
        return True

    # Check containment (one name is a substring of the other).
    if a in b or b in a:
        return True

    # Acronym-expansion check: if one name is a single short token, see if it
    # can be formed from leading characters of the other name's words
    # (e.g. "ERAD" vs "ER-associated degradation").
    words_a = re.findall(r"[a-z]+", a)
    words_b = re.findall(r"[a-z]+", b)

    a_no_sep = re.sub(r"[\s\-_]+", "", a)
    b_no_sep = re.sub(r"[\s\-_]+", "", b)

    if len(words_a) == 1 and len(words_b) >= 2 and _acronym_matches_phrase(a_no_sep, words_b):
        return True
    if len(words_b) == 1 and len(words_a) >= 2 and _acronym_matches_phrase(b_no_sep, words_a):
        return True

    # Word-level overlap.
    set_a = set(words_a)
    set_b = set(words_b)
    if not set_a or not set_b:
        return False
    overlap = len(set_a & set_b) / min(len(set_a), len(set_b))
    return overlap >= 0.6


def deduplicate_chain_claims(
    claims_by_pair: Dict[str, list[Dict[str, Any]]],
) -> Dict[str, list[Dict[str, Any]]]:
    """Remove duplicate function entries within and across interaction pairs."""
    # Collect fingerprints across all pairs for cross-pair dedup.
    global_seen: list[Tuple[str, set[str]]] = []  # (normalised name, significant words)

    def _is_duplicate(claim: Dict[str, Any]) -> bool:
        name = (claim.get("function") or "").strip()
        proc = claim.get("cellular_process", "") or ""
        words = _significant_words(proc)

        for seen_name, seen_words in global_seen:
            if _fuzzy_name_match(name, seen_name) and _word_overlap_ratio(words, seen_words) > 0.6:
                return True
        return False

    result: Dict[str, list[Dict[str, Any]]] = {}

    for pair_key, claims in claims_by_pair.items():
        unique: list[Dict[str, Any]] = []
        for claim in claims:
            if _is_duplicate(claim):
                continue
            name = (claim.get("function") or "").strip()
            words = _significant_words(claim.get("cellular_process", "") or "")
            global_seen.append((name.lower(), words))
            unique.append(claim)
        result[pair_key] = unique

    return result


# ---------------------------------------------------------------------------
# 7. assign_shared_pathway  (pathway consistency)
# ---------------------------------------------------------------------------

def assign_shared_pathway(
    chain_claims: Dict[str, list[Dict[str, Any]]],
    pathway: str,
) -> None:
    """Mutate all claims in a chain group to share the same pathway value."""
    for claims in chain_claims.values():
        for claim in claims:
            claim["pathway"] = pathway


# ---------------------------------------------------------------------------
# 8. format_chain_notation  (display helper)
# ---------------------------------------------------------------------------

def format_chain_notation(
    chain: list[str],
    new_proteins: list[str] | None = None,
    intermediaries: list[str] | None = None,
) -> str:
    """Format a protein chain for human-readable display."""
    new_set = {p.lower() for p in (new_proteins or [])}
    inter_set = {p.lower() for p in (intermediaries or [])}

    parts: list[str] = []
    for name in chain:
        low = name.lower()
        if low in new_set:
            parts.append(f"**{name}**")
        elif low in inter_set:
            parts.append(f"^{name}^")
        else:
            parts.append(name)

    return " \u2192 ".join(parts)
