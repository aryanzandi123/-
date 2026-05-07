#!/usr/bin/env python3
"""
Quick Pathway Assignment
========================
DB-first pathway matching with hierarchy-aware LLM batching. Replaces the
full 7-step pathway pipeline when the user enables "Quick Pathway
Assignment" in the UI.

Chain-aware: claims that share a ``_chain_group`` key describe links in
the same indirect chain (e.g. ATXN3 → FOXO4 → SOD2). They MUST all land
on the same pathway — the LLM is asked to pick **one** pathway per chain
via ``CHAIN_BATCH_ASSIGN_PROMPT``, and a post-hoc consistency pass acts
as a safety net in case the upstream path couldn't group them.

Three-tier matching cascade:
  Tier 1 — Exact name match against existing DB pathways
  Tier 2 — Fuzzy/synonym match (SequenceMatcher >= 0.80, substring 0.50)
  Tier 3 — Batched LLM call with full hierarchy context
"""

import json
import logging
import os
import re
import sys
from collections import defaultdict
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional


def _chain_unify_enabled() -> bool:
    """Gate the chain-pathway unification pass.

    P3.1: now defaults to TRUE. The old "off by default" behavior was a
    misdiagnosis — it conflated two scenarios:
      (a) "flatten EVERY claim on chain-touching interactors to one
          pathway" (bad — erases direct/net claims' independent
          assignments), and
      (b) "rewrite ONLY chain-derived (chain_id-tagged) claims to the
          chain's chosen pathway" (good — exactly the user invariant
          'claims that belong to the chain story share the chain's
          pathway').
    The implementation in ``_unify_all_chain_claims`` only touches
    claims with a non-null ``chain_id``, which makes it scenario (b) by
    construction. Direct/net claims (chain_id=NULL) on the same
    proteins are NEVER mutated. So the gate was overcautious and made
    the screenshot leak that crammed unrelated chains under one
    pathway. Set ``CHAIN_PATHWAY_UNIFY=false`` to restore the old
    skip-all behavior; otherwise selective unification runs.
    """
    return os.getenv("CHAIN_PATHWAY_UNIFY", "true").strip().lower() == "true"

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

FUZZY_THRESHOLD = 0.80
SUBSTRING_MATCH_THRESHOLD = 0.50  # Lowered from 0.70; the old gate rejected
                                  # legitimate prefix matches (e.g. "RNA Binding"
                                  # vs "RNA Binding Protein" scored 0.579).
# Tier-3 quick-classify call: returns one pathway choice per call,
# response shape is `{pathway_name, parent_pathway, hierarchy_level, is_new}`
# (~150 tokens). 1000 leaves ~6× headroom.
LLM_QUICK_CLASSIFY_MAX_OUTPUT_TOKENS = 1000

# Chain-group call: returns ONE pathway decision shared across every
# member of a chain (~150 tokens of JSON). 2048 leaves ~10× headroom
# without the thinking-budget overhead a 8K+ ceiling would imply.
LLM_CHAIN_BATCH_MAX_OUTPUT_TOKENS = 2048

# Standalone batched call: returns up to LLM_BATCH_SIZE pathway
# decisions in one response (each ~100-150 tokens). At LLM_BATCH_SIZE=8
# the typical full-batch response is ~1.0-1.2K tokens; 4096 leaves ~3×
# headroom for the worst case (long pathway names with parents).
LLM_BATCH_ASSIGN_MAX_OUTPUT_TOKENS = 4096

LLM_BATCH_SIZE = 8


# ---------------------------------------------------------------------------
# Parental-consistency gate: keyword topology of the 7 official roots.
# Used by ``_warn_if_off_topic_parent`` to surface obvious LLM
# hallucinations like "Synaptic Plasticity → Metabolism & Bioenergetics".
# ---------------------------------------------------------------------------
_ROOT_TOPIC_KEYWORDS: Dict[str, set] = {
    "Proteostasis": {
        "protein", "fold", "chaperone", "ubiquitin", "proteasome", "autophag",
        "aggreg", "stress response", "unfolded", "erad", "hsp", "stub1",
        "p97", "vcp", "lysosomal degradation",
    },
    "Metabolism & Bioenergetics": {
        "metabol", "atp", "glycolysis", "tca", "krebs", "oxidative",
        "phosphorylation chain", "lipid", "fatty acid", "cholesterol",
        "glucose", "energy", "mitochond", "respiration", "amino acid",
        "nucleotide synthesis", "glycog",
    },
    "Membrane & Transport": {
        "membrane", "vesicle", "endocyt", "exocyt", "transport", "traffick",
        "secret", "endosome", "lysosome", "golgi", "snare", "rab gtpase",
        "vesicular fusion",
    },
    "Genome Maintenance": {
        "dna", "repair", "replication", "chromatin", "genom", "telomer",
        "damage response", "double-strand break", "single-strand break",
        "atm", "atr", "checkpoint", "homologous recombination",
        "non-homologous end joining",
    },
    "Gene Expression": {
        "transcript", "translat", "splicing", "mrna", "rna process",
        "gene express", "ribosome", "polymerase ii", "polii", "tata", "tbp",
        "exon", "intron", "rna binding", "co-translational",
    },
    "Signal Transduction": {
        "signal", "receptor", "kinase", "phosphat", "gtpase", "cascade",
        "second messenger", "calcium signaling", "synapt", "neurotrans",
        "ampa", "nmda", "glutamate", "neuron", "axon", "dendrit",
        "g-protein", "src", "mapk", "pi3k-akt", "mtor",
    },
    "Cytoskeletal Dynamics": {
        "actin", "tubul", "microtub", "intermediate filament", "cytoskelet",
        "motil", "filopod", "lamellipod", "stress fiber", "cofilin", "arp2",
    },
}


def _topic_score(text: str, keywords: set) -> int:
    """Count how many topic keywords appear (case-insensitive substring) in ``text``."""
    if not text:
        return 0
    lower = text.lower()
    return sum(1 for kw in keywords if kw in lower)


def _best_root_for_pathway(pathway_name: str) -> Optional[str]:
    """Return the root whose keyword set best matches ``pathway_name``, or None on tie."""
    if not pathway_name:
        return None
    scores = {root: _topic_score(pathway_name, kws) for root, kws in _ROOT_TOPIC_KEYWORDS.items()}
    best_root, best_score = max(scores.items(), key=lambda kv: kv[1])
    if best_score == 0:
        return None
    # Tie-break: if multiple roots tie at the top, refuse to commit.
    top_roots = [r for r, s in scores.items() if s == best_score]
    if len(top_roots) > 1:
        return None
    return best_root


def _root_of(pathway, db, Pathway, PathwayParent) -> Optional[str]:
    """Walk parent links up to the level-0 root and return its name."""
    seen = set()
    current = pathway
    while current is not None and current.id not in seen:
        seen.add(current.id)
        if current.hierarchy_level == 0:
            return current.name
        link = PathwayParent.query.filter_by(child_pathway_id=current.id).first()
        if not link:
            return None
        current = db.session.get(Pathway, link.parent_pathway_id)
    return None


def _resolve_correct_parent(
    new_pathway_name: str,
    parent_pathway,
    db,
    Pathway,
    PathwayParent,
):
    """Return the Pathway that should actually be used as parent.

    Detects when the LLM has picked a topically-disjoint parent (e.g.
    "Regulation of Synaptic Plasticity → Metabolism & Bioenergetics"
    from the 2026-04-29 ULK1 run) and either:
      * Rejects + reroutes to the keyword-derived root when
        ``STRICT_PATHWAY_PARENT_GATE`` env var is set (default true now
        that quick_assign is the default mode and parental hallucinations
        cause real biological errors).
      * Logs only (legacy soft-warn behavior) when the env var is unset.

    Returns the Pathway object to use as parent. Caller substitutes the
    result for the LLM's pick. When no keyword match exists for the new
    pathway name, returns the original LLM pick unchanged so we don't
    lose data over an inconclusive heuristic.
    """
    if not parent_pathway:
        return parent_pathway
    try:
        parent_root_name = _root_of(parent_pathway, db, Pathway, PathwayParent)
    except Exception:
        return parent_pathway
    if not parent_root_name:
        return parent_pathway
    expected_root = _best_root_for_pathway(new_pathway_name)
    if not expected_root or expected_root == parent_root_name:
        return parent_pathway

    # Mismatch detected. Always log it so operators see the LLM's bad
    # pick in the run output.
    logger.warning(
        "[PARENTAL-CONSISTENCY] '%s' parent root '%s' disagrees with "
        "keyword-derived root '%s' — likely LLM hallucination.",
        new_pathway_name, parent_root_name, expected_root,
    )

    # Hard-reroute behavior (default ON): place the new pathway under
    # the keyword-derived root instead of the LLM's pick. Set
    # STRICT_PATHWAY_PARENT_GATE=false to disable rerouting and keep the
    # legacy log-only behavior.
    strict_mode = os.getenv(
        "STRICT_PATHWAY_PARENT_GATE", "true"
    ).strip().lower() in ("1", "true", "yes")
    if not strict_mode:
        return parent_pathway

    correct_root = (
        Pathway.query
        .filter(db.func.lower(Pathway.name) == expected_root.lower())
        .first()
    )
    if correct_root:
        logger.info(
            "[PARENTAL-CONSISTENCY] Rerouted '%s': new parent '%s' (was '%s')",
            new_pathway_name, correct_root.name, parent_pathway.name,
        )
        return correct_root

    # Keyword-derived root doesn't exist as a Pathway row (shouldn't
    # happen since step1_init_roots seeds them, but defensive). Fall
    # back to the LLM's pick + a louder warning.
    logger.warning(
        "[PARENTAL-CONSISTENCY] Keyword root '%s' not in DB — keeping "
        "LLM's pick '%s' (consider running step1_init_roots).",
        expected_root, parent_pathway.name,
    )
    return parent_pathway


def _warn_if_off_topic_parent(
    new_pathway_name: str,
    parent_pathway,
    db,
    Pathway,
    PathwayParent,
):
    """Compatibility shim: delegates to _resolve_correct_parent and
    returns the resolved parent (which may differ from the input when
    rerouting fires). Kept under the old name so external imports don't
    break; new code should call _resolve_correct_parent directly.
    """
    return _resolve_correct_parent(
        new_pathway_name, parent_pathway, db, Pathway, PathwayParent
    )

QUICK_CLASSIFY_PROMPT = """Given this protein interaction function, select the BEST matching pathway from the existing tree.

Function: {function_description}
Proteins: {protein_a} <-> {protein_b}

Existing pathway tree (by hierarchy level):
{existing_tree}

Rules:
1. You MUST select an existing pathway if ANY reasonable match exists (even partial). Prefer the most specific existing leaf pathway that fits.
2. Only create a NEW pathway as an absolute last resort — if nothing in the tree is relevant at all.
3. When selecting existing: respond with {{"pathway_name": "Exact Existing Name", "is_new": false}}
4. When creating new (LAST RESORT): respond with {{"pathway_name": "New Name", "parent_pathway": "Existing Parent", "hierarchy_level": 2, "is_new": true}}
   - Name must follow the Goldilocks principle: not too broad ("Metabolism"), not too specific ("ATXN3 phosphorylation at Ser473").

Respond with ONLY the JSON.
"""

QUICK_CLASSIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "pathway_name": {"type": "string"},
        "parent_pathway": {"type": "string"},
        "hierarchy_level": {"type": "integer"},
        "is_new": {"type": "boolean"},
    },
    "required": ["pathway_name", "is_new"],
    "additionalProperties": False,
}

BATCH_CLAIM_ASSIGN_PROMPT = """You are a molecular biology expert assigning protein-interaction claims to pathways.

COMPLETE PATHWAY HIERARCHY:
{hierarchy}

---

TOP PRE-SCORED CANDIDATES for this batch (keyword overlap between claim prose
and a curated mechanism vocabulary — higher score = more specific/deeper match):
{candidate_block}

---

CLAIMS:
{claims_json}

STRICT RULES:
1. You MUST pick from the numbered CANDIDATES above unless NONE semantically fits.
2. When two candidates both match, ALWAYS pick the one with the HIGHER keyword_score.
   A higher-score candidate is deeper/more specific; a lower-score candidate is a
   broader parent. Picking the parent over a well-scored child is INCORRECT —
   e.g. "Mitophagy" over "Mitochondrial Quality Control" when the claim mentions
   PINK1/PRKN, or "Cell Cycle" over "DNA Damage Response" when the claim mentions
   cyclins/CDK1/checkpoints.
3. NEVER pick a candidate with keyword_score=0 when one with score ≥ 2 exists.
4. If a claim has a `chain_context` or `mediator_chain` field, let the MECHANISM
   described (function_name + mechanism + effect_description) drive the choice,
   not the chain's endpoint identity.
5. Only create a NEW pathway if the top candidate scores ≤ 1 AND no candidate
   in the hierarchy fits. Verify by rereading the claim first.
6. For new pathways: provide parent_pathway (must be an existing pathway name)
   and hierarchy_level.

Note: Claims that are part of a biological chain are handled in a SEPARATE prompt,
so every claim here is independent of the others.

Respond with JSON matching the schema exactly.
"""

BATCH_CLAIM_ASSIGN_SCHEMA = {
    "type": "object",
    "properties": {
        "assignments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "claim_id": {"type": "integer"},
                    "pathway_name": {"type": "string"},
                    "pathway_id": {"type": "integer"},
                    "is_new": {"type": "boolean"},
                    "parent_pathway": {"type": "string"},
                    "hierarchy_level": {"type": "integer"},
                    "reasoning": {"type": "string"},
                },
                "required": ["claim_id", "pathway_name", "is_new"],
            },
        }
    },
    "required": ["assignments"],
}

CHAIN_BATCH_ASSIGN_PROMPT = """You are a molecular biology expert assigning a chain of protein-interaction claims to ONE pathway.

COMPLETE PATHWAY HIERARCHY:
{hierarchy}

---

TOP PRE-SCORED CANDIDATES for this chain (keyword overlap between the chain's
combined claim prose and a curated mechanism vocabulary — higher score = more
specific/deeper match):
{candidate_block}

---

The following scientific claims are part of the SAME biological chain.
ALL claims below MUST be assigned to the SAME single pathway.

Chain: {chain_display}

CLAIMS:
{claims_json}

STRICT RULES:
1. ALL claims receive the SAME pathway — they are mechanistically linked in one chain.
2. You MUST pick from the numbered CANDIDATES above unless NONE semantically fits.
3. When two candidates both match, ALWAYS pick the one with the HIGHER keyword_score.
   Higher score = deeper/more specific; lower score = broader parent.
   Choosing the parent over a well-scored child is INCORRECT.
4. NEVER pick a candidate with keyword_score=0 when one with score ≥ 2 exists.
5. Only create a NEW pathway if the top candidate scores ≤ 1 AND no candidate
   in the hierarchy fits the chain's shared biological theme.

Respond with JSON matching the schema exactly."""

CHAIN_BATCH_ASSIGN_SCHEMA = {
    "type": "object",
    "properties": {
        "pathway_name": {"type": "string"},
        "is_new": {"type": "boolean"},
        "parent_pathway": {"type": ["string", "null"]},
        "hierarchy_level": {"type": ["integer", "null"]},
        "reasoning": {"type": "string"},
    },
    "required": ["pathway_name", "is_new", "reasoning"],
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _normalize_for_matching(name: str) -> str:
    """Normalize pathway name for fuzzy comparison.

    1. Lowercase.
    2. Map any whitespace run (spaces, tabs, newlines) to a single space —
       this is the step that was missing in the prior version, which kept
       ``"Autophagy  Receptor"`` (double space) distinct from
       ``"Autophagy Receptor"``.
    3. Strip non-alphanumeric characters except that one canonical space.
    4. Strip leading/trailing whitespace.
    """
    spaced = re.sub(r'\s+', ' ', name.lower())
    cleaned = re.sub(r'[^a-z0-9 ]', '', spaced)
    return re.sub(r'\s+', ' ', cleaned).strip()


def _find_best_match(hint: str, all_pathways) -> Optional[Any]:
    """Three-tier matching: exact -> fuzzy -> None.

    Among multiple fuzzy matches, prefers highest hierarchy_level (most specific).
    """
    if not hint or not all_pathways:
        return None

    hint_lower = hint.strip().lower()
    hint_norm = _normalize_for_matching(hint)

    # Tier 1: exact match (case-insensitive)
    for pw in all_pathways:
        if pw.name and pw.name.strip().lower() == hint_lower:
            return pw

    # Tier 2: fuzzy matching
    best_match = None
    best_score = 0.0

    for pw in all_pathways:
        if not pw.name:
            continue
        pw_norm = _normalize_for_matching(pw.name)

        # Exact normalized match (handles punctuation/spacing differences)
        if hint_norm == pw_norm:
            return pw

        # Substring containment (bidirectional)
        if hint_norm in pw_norm or pw_norm in hint_norm:
            score = min(len(hint_norm), len(pw_norm)) / max(len(pw_norm), len(hint_norm), 1)
            if score > best_score and score > SUBSTRING_MATCH_THRESHOLD:
                best_score = score
                best_match = pw
                continue

        # Sequence similarity
        ratio = SequenceMatcher(None, hint_norm, pw_norm).ratio()
        if ratio > best_score and ratio >= FUZZY_THRESHOLD:
            best_score = ratio
            best_match = pw

    return best_match


def _match_from_description(func_desc: str, all_pathways) -> Optional[Any]:
    """Extract pathway-like terms from function descriptions and match individually."""
    if not func_desc or not all_pathways:
        return None
    # Try each semicolon-separated function independently
    for part in func_desc.split(';'):
        part = part.strip()
        if not part:
            continue
        match = _find_best_match(part, all_pathways)
        if match:
            return match
        # Extract capitalized multi-word phrases (likely pathway names)
        phrases = re.findall(r'[A-Z][a-z]+(?:\s+[A-Z][a-z]+)+', part)
        for phrase in phrases:
            match = _find_best_match(phrase, all_pathways)
            if match:
                return match
    return None


def _serialize_pathway_hierarchy(db, Pathway, PathwayParent) -> str:
    """Serialize full pathway hierarchy as indented tree text for LLM context."""
    all_pws = Pathway.query.all()
    all_links = PathwayParent.query.all()

    pw_by_id = {pw.id: pw for pw in all_pws}
    children_of: Dict[int, List[int]] = defaultdict(list)
    has_parent = set()

    for link in all_links:
        children_of[link.parent_pathway_id].append(link.child_pathway_id)
        has_parent.add(link.child_pathway_id)

    # Find roots: no parent link OR hierarchy_level == 0
    root_ids = []
    for pw in all_pws:
        if pw.id not in has_parent:
            root_ids.append(pw.id)

    # Sort roots alphabetically
    root_ids.sort(key=lambda pid: (pw_by_id[pid].name or "").lower())

    lines: List[str] = []
    visited: set = set()

    def _dfs(pw_id: int, depth: int) -> None:
        if pw_id in visited or depth > 20:
            return
        visited.add(pw_id)
        pw = pw_by_id.get(pw_id)
        if not pw:
            return
        indent = "  " * depth
        tags = []
        if depth == 0:
            tags.append("ROOT")
        if getattr(pw, "is_leaf", False):
            tags.append("LEAF")
        if getattr(pw, "ontology_id", None):
            tags.append(pw.ontology_id)
        tag_str = f" [{', '.join(tags)}]" if tags else ""
        lines.append(f"{indent}[{pw.id}] {pw.name}{tag_str}")

        child_ids = children_of.get(pw_id, [])
        child_ids.sort(key=lambda cid: (pw_by_id.get(cid, None) and pw_by_id[cid].name or "").lower())
        for cid in child_ids:
            _dfs(cid, depth + 1)

    for rid in root_ids:
        _dfs(rid, 0)

    return "\n".join(lines)


def _acquire_pathway_creation_lock(db: Any, pathway_name: str) -> bool:
    """Acquire a PostgreSQL advisory lock keyed on the lowercased pathway
    name, serializing concurrent "create pathway X" attempts within a
    single transaction.

    Before this guard, two threads could both pass the check-then-insert
    race-guard query with no existing row, both try to insert, and then
    one would hit the UNIQUE constraint collision. The old recovery path
    (``try/except IntegrityError`` → re-fetch) worked most of the time
    but rolled back the *entire* session on the losing side, potentially
    losing unrelated pending mutations from the same batch.

    With ``pg_advisory_xact_lock``, the second thread blocks on the name
    until the first thread's transaction commits; by the time the second
    thread's check-query runs, the new row is visible and we return it
    instead of racing to insert a duplicate. The lock is released
    automatically at transaction commit/rollback — no explicit cleanup
    required.

    Returns ``True`` when the lock was acquired (or when the backend is
    SQLite and we're intentionally skipping), ``False`` when acquisition
    failed and the caller should fall through to the legacy recovery
    path. The function never raises — failure to acquire an advisory
    lock must not block pathway creation, only downgrade it from
    race-free to race-recovery.

    No-op on non-PostgreSQL backends. SQLite is only used in tests; it
    holds a per-file write lock for its transactions already, and the
    in-memory test databases don't see the concurrency patterns this
    guard protects against.
    """
    try:
        dialect = db.engine.dialect.name
    except Exception as exc:
        logger.debug(
            f"Pathway creation advisory lock: could not inspect dialect "
            f"(continuing without lock): {type(exc).__name__}: {exc}"
        )
        return False
    if dialect != "postgresql":
        return True  # intentional no-op on non-Postgres backends
    try:
        db.session.execute(
            db.text("SELECT pg_advisory_xact_lock(hashtext(lower(:name)))"),
            {"name": pathway_name},
        )
        return True
    except Exception as exc:
        logger.warning(
            f"Pathway creation advisory lock failed for '{pathway_name}': "
            f"{type(exc).__name__}: {exc} — falling back to "
            f"IntegrityError recovery path"
        )
        return False


def _resolve_or_create_pathway(
    pathway_name: str,
    is_new: bool,
    parent_name: str,
    hierarchy_level: Optional[int],
    all_pathways: list,
    db,
    Pathway,
    PathwayParent,
    pathway_id: Optional[int] = None,
) -> Optional[Any]:
    """Resolve an existing pathway or create a new one. Returns Pathway ORM object."""
    if not pathway_name:
        return None

    # ID-based lookup takes precedence (eliminates name-matching ambiguity)
    if pathway_id is not None:
        existing = Pathway.query.get(pathway_id)
        if existing:
            return existing

    # Strip any [id] prefix the LLM may have included in the name
    stripped = re.sub(r'^\[\d+\]\s*', '', pathway_name).strip()
    if stripped:
        pathway_name = stripped

    # Try exact match first (fast path — no lock needed for an existing row)
    if not is_new:
        existing = Pathway.query.filter(
            db.func.lower(Pathway.name) == pathway_name.lower()
        ).first()
        if existing:
            return existing
        # Fuzzy fallback
        match = _find_best_match(pathway_name, all_pathways)
        if match:
            return match

    # From here on, we're on the "maybe-create" path. Acquire a Postgres
    # advisory lock keyed on the lowercased name so concurrent workers
    # attempting to create the same pathway serialize at this line rather
    # than racing into the UNIQUE-constraint collision below. The lock is
    # released automatically when the enclosing transaction commits.
    _acquire_pathway_creation_lock(db, pathway_name)

    # Check if name already exists (race guard — race-free under the lock
    # on Postgres, best-effort on other dialects with the IntegrityError
    # recovery still providing a safety net).
    existing = Pathway.query.filter(db.func.lower(Pathway.name) == pathway_name.lower()).first()
    if existing:
        return existing

    # Create new pathway
    parent_pw = None
    if parent_name:
        parent_pw = Pathway.query.filter(db.func.lower(Pathway.name) == parent_name.lower()).first()

    # Parental-consistency gate: when the LLM picks a parent whose root
    # doesn't share any topical keyword with the new pathway's name,
    # reroute the pathway to the keyword-derived root (or log + keep
    # the LLM's pick when STRICT_PATHWAY_PARENT_GATE=false). This is
    # the fix for the "Synaptic Plasticity → Metabolism & Bioenergetics"
    # hallucination observed on the 2026-04-29 ULK1 run.
    parent_pw = _resolve_correct_parent(
        pathway_name, parent_pw, db, Pathway, PathwayParent
    )

    if hierarchy_level is None:
        hierarchy_level = (parent_pw.hierarchy_level + 1) if parent_pw else 1

    new_pw = Pathway(
        name=pathway_name,
        hierarchy_level=hierarchy_level,
        is_leaf=True,
        ai_generated=True,
        usage_count=0,
    )

    try:
        from scripts.pathway_v2.ontology_mappings import enrich_pathway_with_ontology
        match = enrich_pathway_with_ontology(pathway_name)
        if match:
            new_pw.ontology_id = match["ontology_id"]
            new_pw.ontology_source = match["ontology_source"]
            new_pw.canonical_term = match.get("canonical_name")
    except ImportError as e:
        # ontology_mappings may be genuinely optional in some deployments.
        logger.info(
            f"Ontology enrichment module unavailable for '{pathway_name}': {e}"
        )
    except Exception as e:
        # Anything else (malformed mapping data, KeyError in match dict,
        # network issue inside enrich_pathway_with_ontology) is a real
        # problem — surface it so we don't silently ship pathways without
        # ontology IDs.
        logger.warning(
            f"Ontology enrichment failed for '{pathway_name}': "
            f"{type(e).__name__}: {e}"
        )

    try:
        db.session.add(new_pw)
        db.session.flush()

        if parent_pw:
            link = PathwayParent(
                child_pathway_id=new_pw.id,
                parent_pathway_id=parent_pw.id,
                relationship_type="is_a",
                confidence=0.85,
                source="AI",
            )
            db.session.add(link)
            if parent_pw.is_leaf:
                parent_pw.is_leaf = False
            parent_ancestors = parent_pw.ancestor_ids if isinstance(parent_pw.ancestor_ids, list) else []
            new_pw.ancestor_ids = [parent_pw.id] + parent_ancestors

        db.session.flush()
        logger.info(f"  Created new pathway '{pathway_name}' (level={hierarchy_level}, parent='{parent_name}')")
        # Invalidate the Gemini hierarchy cache: a new pathway just landed,
        # so the pre-built system instruction (which serialised the entire
        # hierarchy at cache-create time) is now stale. Without this, the
        # next LLM call in this same run would still see the old hierarchy
        # and couldn't assign claims to the pathway we just created. Best-
        # effort; any error here is non-fatal.
        try:
            _invalidate_hierarchy_cache()
        except Exception as _inv_exc:
            logger.debug(f"Hierarchy cache invalidation skipped: {_inv_exc}")
        return new_pw

    except Exception as e:
        db.session.rollback()
        from sqlalchemy.exc import IntegrityError
        if isinstance(e, IntegrityError):
            logger.info(f"Race condition on pathway '{pathway_name}', fetching existing")
        else:
            logger.warning(f"Failed to create pathway '{pathway_name}': {e}")
        return Pathway.query.filter(db.func.lower(Pathway.name) == pathway_name.lower()).first()


# Module-level cache-name tracker for _invalidate_hierarchy_cache. The
# quick_assign main loop sets this when it obtains a Vertex AI explicit
# cache; creation of a new pathway later in the same run needs to drop
# that cache so the next LLM call doesn't see a stale hierarchy.
_ACTIVE_HIERARCHY_CACHE_NAME: Optional[str] = None


def _register_hierarchy_cache(name: Optional[str]) -> None:
    """Record the current Gemini cache name so later code can invalidate it."""
    global _ACTIVE_HIERARCHY_CACHE_NAME
    _ACTIVE_HIERARCHY_CACHE_NAME = name


def _invalidate_hierarchy_cache() -> None:
    """Drop the active hierarchy cache so the next LLM call rebuilds it.

    Safe to call even if no cache is registered. The actual cache deletion
    on Vertex AI is best-effort — even if the delete call fails, clearing
    the registered name on our side is enough: the next quick_assign run
    will request a fresh cache and all subsequent calls point at the new
    one. The TTL on the server expires the stale cache naturally.
    """
    global _ACTIVE_HIERARCHY_CACHE_NAME
    if not _ACTIVE_HIERARCHY_CACHE_NAME:
        return
    _stale = _ACTIVE_HIERARCHY_CACHE_NAME
    _ACTIVE_HIERARCHY_CACHE_NAME = None
    logger.info(f"Invalidated hierarchy cache '{_stale}' (new pathway created)")


def _create_pathway_via_llm(
    func_desc: str,
    protein_a: str,
    protein_b: str,
    existing_tree_str: str,
    db,
    Pathway,
    PathwayParent,
) -> Optional[Any]:
    """Tier 3: LLM call to classify into existing pathway, or create one as last resort."""
    from scripts.pathway_v2.llm_utils import _call_gemini_json

    prompt = QUICK_CLASSIFY_PROMPT.format(
        function_description=func_desc[:500],
        protein_a=protein_a,
        protein_b=protein_b,
        existing_tree=existing_tree_str[:3000],
    )

    try:
        resp = _call_gemini_json(
            prompt,
            response_json_schema=QUICK_CLASSIFY_SCHEMA,
            thinking_level="low",
            disable_afc=True,
            max_output_tokens=LLM_QUICK_CLASSIFY_MAX_OUTPUT_TOKENS,
            model="gemini-3-flash-preview",
        )
    except Exception as e:
        logger.warning(
            f"LLM quick-classify call failed for protein pair "
            f"{protein_a}/{protein_b}: {type(e).__name__}: {e}"
        )
        return None

    if not isinstance(resp, dict):
        # safe_extract_json returns {} on parse failure — treat as a loud
        # failure, not a silent skip, so we can triage parse-rate regressions.
        logger.warning(
            f"LLM quick-classify returned non-dict response "
            f"(type={type(resp).__name__}) for {protein_a}/{protein_b}"
        )
        return None
    if not resp:
        logger.warning(
            f"LLM quick-classify returned empty dict (likely JSON parse "
            f"failure) for {protein_a}/{protein_b}"
        )
        return None

    all_pw = Pathway.query.all()
    return _resolve_or_create_pathway(
        # Default is_new=False: when the LLM omits the flag, we bias toward
        # matching an existing pathway — "STRONGLY prefer existing" is the
        # documented rule at the top of the file, and the prior default of
        # True silently created new pathways whenever the model forgot to
        # include the key.
        pathway_name=(resp.get("pathway_name") or "").strip(),
        is_new=resp.get("is_new", False),
        parent_name=(resp.get("parent_pathway") or "").strip(),
        hierarchy_level=resp.get("hierarchy_level"),
        all_pathways=all_pw,
        db=db,
        Pathway=Pathway,
        PathwayParent=PathwayParent,
    )


# ---------------------------------------------------------------------------
# Chain-Grouping Helpers
# ---------------------------------------------------------------------------

def _extract_chain_group(claim) -> Optional[str]:
    """Extract a stable group key for a chain-derived claim.

    Lookup order (priority HIGH → LOW):
      1. **DB ``claim.chain_id``** — authoritative when present. Two claims
         under the same IndirectChain row MUST share a group key so the
         LLM batches them together; using the in-payload tag first would
         split a single chain across multiple groups when some hops were
         tagged at post-processing time and others were created later
         (via ``scripts/audit_chain_completeness.py --repair`` or via
         a fresh sync after L1+L4 landed). The synthetic key
         ``"db_chain:<id>"`` cannot collide with the in-payload format
         (which uses chain-protein-joined strings).
      2. ``claim.interaction.data.functions[*]._chain_group`` — set by the
         post-processor's ``chain_group_tagging`` stage during a fresh run.
         Used when ``chain_id`` is absent (e.g. legacy claims pre-dating
         the chain_id column being populated).
      3. ``claim.raw_function_data._chain_group`` — the function dict
         stored alongside the claim at creation time.
      4. ``claim.context_data._chain_group`` — alt location for chain info.
    """
    # 1. DB chain_id — authoritative.
    chain_id = getattr(claim, "chain_id", None)
    if chain_id is not None:
        return f"db_chain:{chain_id}"
    # 2. Interaction-level data (functions array in the JSONB blob).
    if claim.interaction and claim.interaction.data:
        for fn in (claim.interaction.data.get("functions") or []):
            if isinstance(fn, dict) and fn.get("_chain_group"):
                return fn["_chain_group"]
    # 3. Claim's own raw_function_data (the full function dict at creation).
    raw = getattr(claim, "raw_function_data", None)
    if raw and isinstance(raw, dict) and raw.get("_chain_group"):
        return raw["_chain_group"]
    # 4. context_data (may contain chain info).
    ctx = getattr(claim, "context_data", None)
    if ctx and isinstance(ctx, dict) and ctx.get("_chain_group"):
        return ctx["_chain_group"]
    return None


def _score_pathway_candidates(claim_text: str, all_pathways: list, *, top_k: int = 6) -> list:
    """Rank DB pathways by keyword overlap with a batch's combined claim text.

    Uses ``utils.pathway_content_validator``'s curated keyword map as the
    pre-LLM prefilter. Returns up to ``top_k`` (pathway, score, is_leaf,
    parent_name) tuples, sorted by score (desc) then by ``hierarchy_level``
    (desc — deeper first). Zero-score pathways are still appended at the
    tail so the LLM can fall back to broader matches when keywords miss.

    Why this exists: Gemini 3 Flash on the full serialised hierarchy tree
    tends to pick the first matching parent (e.g. "Mitochondrial Quality
    Control" over "Mitophagy") because the hierarchy display gives all
    siblings equal visual weight. Showing it pre-scored candidates with
    explicit keyword_score + leaf flag makes the right answer obvious
    and matches the drift-detector's own scoring, so the first-pass
    assignment agrees with the post-hoc verifier.
    """
    try:
        from utils.pathway_content_validator import _SEED_PATHWAY_KEYWORDS, _compiled_pattern
    except Exception:
        return []
    if not claim_text or not all_pathways:
        return []
    scored: dict[str, int] = {}
    for seed_name in _SEED_PATHWAY_KEYWORDS:
        pat = _compiled_pattern(seed_name)
        if pat is None:
            continue
        hits = len(pat.findall(claim_text))
        if hits:
            scored[seed_name] = hits
    # Resolve seed names to concrete DB pathways so the LLM gets names it
    # can actually return (the seed map may use canonical names that
    # differ from what's in the DB). Use the same fuzzy matcher the rest
    # of quick_assign already trusts.
    db_hit: list[tuple] = []
    seen_ids = set()
    for seed_name, score in sorted(scored.items(), key=lambda kv: -kv[1]):
        match = _find_best_match(seed_name, all_pathways)
        if match is None or getattr(match, "id", None) in seen_ids:
            continue
        seen_ids.add(match.id)
        db_hit.append((match, score))
        if len(db_hit) >= top_k:
            break
    # Pad out with any remaining pathways (score=0) so broad fallbacks
    # are still visible — important for claims whose mechanism doesn't
    # intersect the seed vocabulary.
    if len(db_hit) < top_k:
        remaining = [
            pw for pw in all_pathways
            if getattr(pw, "id", None) not in seen_ids and pw.name
        ]
        # Sort remaining by hierarchy_level desc (prefer deeper) then name.
        remaining.sort(
            key=lambda pw: (-int(getattr(pw, "hierarchy_level", 0) or 0), pw.name),
        )
        for pw in remaining[: top_k - len(db_hit)]:
            db_hit.append((pw, 0))
            seen_ids.add(getattr(pw, "id", None))
    return [
        (pw, score,
         bool(getattr(pw, "is_leaf", False)),
         int(getattr(pw, "hierarchy_level", 0) or 0))
        for pw, score in db_hit
    ]


def _combined_claim_text(claims_batch) -> str:
    """Join function_name + mechanism + effect_description for keyword scoring."""
    chunks: list[str] = []
    for c in claims_batch:
        for attr in ("function_name", "mechanism", "effect_description"):
            v = getattr(c, attr, None)
            if v:
                chunks.append(str(v))
    return " ".join(chunks)


def _format_candidate_block(ranked: list) -> str:
    """Human-readable numbered block for the prompt template.

    Format: ``#N [score=S, leaf=Y/N, level=L] Pathway Name (parent: ...)``.
    """
    if not ranked:
        return "  (no pre-scored candidates — fall back to the full hierarchy)"
    lines: list[str] = []
    for i, (pw, score, is_leaf, level) in enumerate(ranked, 1):
        parent_hint = ""
        try:
            parents = getattr(pw, "parents", None) or []
            if parents:
                first = parents[0]
                pname = getattr(getattr(first, "parent", None), "name", None)
                if pname:
                    parent_hint = f"; parent: {pname}"
        except Exception:
            pass
        leaf_flag = "leaf" if is_leaf else "internal"
        lines.append(
            f"  #{i} [score={score}, {leaf_flag}, level={level}] "
            f"{pw.name}{parent_hint}"
        )
    return "\n".join(lines)


def _build_claim_info(claim) -> Dict[str, Any]:
    """Build the JSON-serialisable dict sent to the LLM for a single claim."""
    info: Dict[str, Any] = {
        "claim_id": claim.id,
        "function_name": claim.function_name or "",
        "mechanism": (claim.mechanism or "")[:300],
        "effect_description": (claim.effect_description or "")[:200],
        "protein_a": (
            claim.interaction.protein_a.symbol
            if claim.interaction and claim.interaction.protein_a else "?"
        ),
        "protein_b": (
            claim.interaction.protein_b.symbol
            if claim.interaction and claim.interaction.protein_b else "?"
        ),
    }
    if claim.interaction and claim.interaction.data:
        chain_ctx = claim.interaction.data.get("chain_context")
        if chain_ctx:
            info["chain_context"] = chain_ctx
        mediator = claim.interaction.data.get("mediator_chain")
        if mediator:
            info["mediator_chain"] = mediator
    elif claim.interaction:
        # Fallback: JSONB blob absent — read from computed chain properties
        if hasattr(claim.interaction, 'computed_mediator_chain'):
            mediator = claim.interaction.computed_mediator_chain
            if mediator:
                info["mediator_chain"] = mediator
        if hasattr(claim.interaction, 'computed_upstream_interactor'):
            info["upstream"] = claim.interaction.computed_upstream_interactor
    return info


def _try_db_match(claim, all_pathways) -> Optional[Any]:
    """Attempt Tier 1 (exact) and Tier 2 (fuzzy) DB matching for a single claim."""
    matched = None

    if claim.pathway_name:
        matched = _find_best_match(claim.pathway_name, all_pathways)

    if not matched and claim.function_name:
        matched = _find_best_match(claim.function_name, all_pathways)

    if not matched:
        desc = claim.mechanism or claim.effect_description or ""
        if desc:
            matched = _match_from_description(desc, all_pathways)

    return matched


def _pick_majority_pathway_id(members: List[Any]) -> Optional[int]:
    """Pure helper: majority-vote dominant pathway for a list of claims.

    Used by the protein-level consistency pass (where every claim is
    treated equally). Ties broken by smallest ``pathway_id`` for
    determinism in tests.
    """
    pw_counts: Dict[int, int] = {}
    for m in members:
        pw_id = getattr(m, "pathway_id", None)
        if pw_id is None:
            continue
        pw_counts[pw_id] = pw_counts.get(pw_id, 0) + 1
    if not pw_counts:
        return None
    max_count = max(pw_counts.values())
    return min(pw_id for pw_id, count in pw_counts.items() if count == max_count)


def _pick_chain_dominant_pathway_id(members: List[Any]) -> Optional[int]:
    """Pure helper: pick the dominant pathway_id for a chain group.

    Priority:
      1. Prefer the pathway of any claim with ``function_context == "net"``
         (these are the end-to-end chain claims that describe the biological
         endpoint and are the canonical label for the whole chain).
      2. Fall back to majority vote over non-null ``pathway_id`` values.
      3. Return None if the group has no resolved pathways at all.

    This function does NOT touch the database or mutate claims — the caller
    applies the reassignment. Isolating the decision makes the chain
    consistency rule testable without a Flask app context.
    """
    net_members = [
        m for m in members
        if (getattr(m, "function_context", "") or "").lower() == "net"
        and getattr(m, "pathway_id", None) is not None
    ]
    if net_members:
        return net_members[0].pathway_id

    # No net claim in the group → fall back to simple majority vote.
    return _pick_majority_pathway_id(members)


def _merge_claim_into_survivor(donor: Any, survivor: Any) -> None:
    """Merge ``donor`` claim's content into ``survivor`` and delete donor.

    Shared by ``_assign_claim_pathway_safe`` (F3) and ``_unify_all_chain_claims``
    (P3.1). When two claims represent the same identity under the COALESCE
    unique index ``(interaction_id, function_name, pathway_name,
    function_context)``, we cannot keep both — but we also do not want to
    silently lose the donor's biology. Instead we union evidence, pmids,
    specific_effects, biological_consequences; keep the longer prose on
    mechanism/effect_description; keep the higher confidence; and merge
    context_data + raw_function_data. Donor row is then marked deleted in
    the active SQLAlchemy session (the caller is responsible for the
    eventual flush/commit).

    This is an in-place mutation of ``survivor``. Caller MUST stop using
    the donor object after this call returns; further attribute reads on
    the deleted row are undefined.
    """
    from models import db

    # Evidence — union by paper_title (lower-cased).
    surv_ev = list(survivor.evidence or [])
    surv_titles = {
        (e.get("paper_title") or "").lower()
        for e in surv_ev if isinstance(e, dict)
    }
    for ev in donor.evidence or []:
        if not isinstance(ev, dict):
            continue
        title = (ev.get("paper_title") or "").lower()
        if title and title not in surv_titles:
            surv_ev.append(ev)
            surv_titles.add(title)
    survivor.evidence = surv_ev

    # PMIDs — union.
    surv_pmids = set(survivor.pmids or []) | set(donor.pmids or [])
    survivor.pmids = sorted(p for p in surv_pmids if p)

    # Prefer longer prose on mechanism / effect_description.
    for field in ("mechanism", "effect_description"):
        donor_val = getattr(donor, field) or ""
        surv_val = getattr(survivor, field) or ""
        if len(donor_val) > len(surv_val):
            setattr(survivor, field, donor_val)

    # Union JSONB arrays (biological_consequences, specific_effects).
    for field in ("biological_consequences", "specific_effects"):
        surv_arr = list(getattr(survivor, field) or [])
        surv_strs = {str(x) for x in surv_arr}
        for item in getattr(donor, field) or []:
            if str(item) not in surv_strs:
                surv_arr.append(item)
                surv_strs.add(str(item))
        setattr(survivor, field, surv_arr)

    # Keep higher confidence.
    donor_conf = float(donor.confidence or 0)
    surv_conf = float(survivor.confidence or 0)
    if donor_conf > surv_conf:
        survivor.confidence = donor.confidence

    # Merge context_data (donor fills in missing keys only).
    if donor.context_data:
        surv_ctx = dict(survivor.context_data or {})
        for k, v in donor.context_data.items():
            surv_ctx.setdefault(k, v)
        survivor.context_data = surv_ctx

    # Keep richer raw_function_data.
    donor_raw = donor.raw_function_data or {}
    surv_raw = survivor.raw_function_data or {}
    if len(str(donor_raw)) > len(str(surv_raw)):
        survivor.raw_function_data = donor_raw

    db.session.delete(donor)


def _assign_claim_pathway_safe(claim: Any, pw: Any) -> bool:
    """Assign ``pw`` to ``claim``, merging into a collision row if one exists.

    The schema's COALESCE index treats two rows as the same key when they
    share ``(interaction_id, function_name, COALESCE(pathway_name, ''),
    COALESCE(function_context, ''))``. Updating a claim's ``pathway_name``
    to a value that would collide with another claim on the same
    interaction therefore hits an ``IntegrityError`` at flush time —
    and because quick_assign batches many updates, one bad flush kills
    the entire pass.

    F3: previously, this helper SKIPPED the assignment on collision and
    left the claim for "step7 / manual review." Step 7 didn't actually
    repair these, so the user saw 11/158 unresolved CLAIM FAILED warnings
    per run. Now: when a collision is detected, the donor (``claim``)
    is merged into the existing collision row via
    ``_merge_claim_into_survivor`` — evidence/pmids/cascades unioned,
    longer prose kept, donor deleted. The caller still receives True so
    its bookkeeping reflects "the pathway was successfully populated for
    this identity," even though the surviving row is the collision, not
    the donor. The caller MUST NOT read attributes on ``claim`` after
    this returns when the merge path fired (donor is deleted).

    Outside a Flask app context (pure unit tests with fake claim
    objects), the DB-backed collision check is unavailable. Fall
    through to direct assignment in that case — the caller's test
    harness is responsible for not constructing colliding fakes.

    Returns True when the assignment was applied (direct or via merge),
    False only when ``pw`` is invalid.
    """
    if pw is None or getattr(pw, "id", None) is None:
        return False

    # No-op: same pathway already set. Always safe.
    if claim.pathway_id == pw.id:
        return True

    # Unit-test escape hatch: no Flask app context → skip the DB
    # collision check and fall through to direct assignment.
    try:
        from flask import has_app_context
        if not has_app_context():
            claim.pathway_id = pw.id
            claim.pathway_name = pw.name
            return True
    except ImportError:
        # Flask not installed — also test context, just assign.
        claim.pathway_id = pw.id
        claim.pathway_name = pw.name
        return True

    import sqlalchemy as sa
    from models import InteractionClaim

    # 2026-04-30 — collision filter mirrors the 5-col uq_claim_interaction_fn_pw_ctx
    # index (interaction_id, function_name, COALESCE(pathway_name,''),
    # COALESCE(function_context,''), COALESCE(chain_id, 0)). Including
    # chain_id is what lets a chain-derived claim (chain_id=N) coexist
    # with a direct claim (chain_id=NULL) about the same function — the
    # DB allows them as distinct rows, and runtime no longer spuriously
    # merges across distinct biological cascades.
    collision = (
        InteractionClaim.query
        .filter(InteractionClaim.interaction_id == claim.interaction_id)
        .filter(InteractionClaim.function_name == claim.function_name)
        .filter(sa.func.coalesce(InteractionClaim.pathway_name, "") == (pw.name or ""))
        .filter(
            sa.func.coalesce(InteractionClaim.function_context, "")
            == ((claim.function_context or ""))
        )
        .filter(
            sa.func.coalesce(InteractionClaim.chain_id, 0)
            == (claim.chain_id or 0)
        )
        .filter(InteractionClaim.id != claim.id)
        .first()
    )
    if collision is not None:
        # F3: merge donor into survivor instead of skipping.
        try:
            _merge_claim_into_survivor(donor=claim, survivor=collision)
            logger.info(
                f"  [CLAIM MERGED] interaction_id={claim.interaction_id} "
                f"function={claim.function_name!r} pathway={pw.name!r} "
                f"— merged donor claim {claim.id} into existing claim "
                f"{collision.id} (evidence/pmids/cascades unioned)."
            )
            return True
        except Exception as merge_exc:
            # If merge fails for any reason (DB issue, model mismatch),
            # fall back to the old skip behavior so the run continues.
            # This is rare; the caller's "failed" counter still surfaces it.
            logger.warning(
                f"  [CLAIM MERGE FAILED] interaction_id={claim.interaction_id} "
                f"function={claim.function_name!r} pathway={pw.name!r}: "
                f"{type(merge_exc).__name__}: {merge_exc} — falling back "
                "to skip-assignment so the pass can continue."
            )
            return False

    claim.pathway_id = pw.id
    claim.pathway_name = pw.name
    return True


def _apply_consistency_pass(
    claims: List[Any],
    group_key_fn: Callable[[Any], Optional[str]],
    dominant_picker: Callable[[List[Any]], Optional[int]],
    Pathway_cls: Any,
    sync_fn: Callable[[Any, str], None],
) -> int:
    """Generic consistency pass over a list of claims.

    Groups ``claims`` by the string returned from ``group_key_fn`` (claims
    whose key is ``None`` are skipped), then for each group whose members
    span more than one distinct ``pathway_id``:
      1. Calls ``dominant_picker(members)`` to decide the canonical pathway.
      2. Fetches the Pathway row via ``Pathway_cls.query.get``.
      3. Reassigns every non-dominant member (updates ``pathway_id`` and
         ``pathway_name``, then calls ``sync_fn(claim, pathway_name)`` so
         the JSONB mirror stays in sync).

    Returns the number of claims whose pathway was changed.

    This helper is the shared core of the protein-level and chain-level
    consistency passes — both previously inlined the same group-key /
    dominant-vote / reassign boilerplate. Extracting it honors the
    "reuse first" principle in CLAUDE.md and makes each caller a short
    configuration of this one shape.
    """
    groups: Dict[str, List[Any]] = {}
    for claim in claims:
        if not getattr(claim, "pathway_id", None):
            continue
        key = group_key_fn(claim)
        if not key:
            continue
        groups.setdefault(key, []).append(claim)

    fixes = 0
    for _key, members in groups.items():
        distinct_pw_ids = {m.pathway_id for m in members if m.pathway_id is not None}
        if len(distinct_pw_ids) <= 1:
            continue  # already consistent

        dominant_pw_id = dominant_picker(members)
        if dominant_pw_id is None:
            continue

        dominant_pw = Pathway_cls.query.get(dominant_pw_id)
        if not dominant_pw:
            continue

        for m in members:
            if m.pathway_id == dominant_pw_id:
                continue
            # H1: safe-update — skip the reassignment if it would
            # collide with another claim already on the target pathway.
            if _assign_claim_pathway_safe(m, dominant_pw):
                sync_fn(m, dominant_pw.name)
                fixes += 1
    return fixes


def _protein_fn_group_key(claim: Any) -> Optional[str]:
    """Build the ``protein_b||function_name`` key used by the protein-level
    consistency pass. Returns None when either field is missing so the
    consistency pass skips the claim cleanly.
    """
    protein_name = ""
    interaction = getattr(claim, "interaction", None)
    if interaction and getattr(interaction, "protein_b", None):
        protein_name = interaction.protein_b.symbol or ""
    fn_name = (getattr(claim, "function_name", "") or "").strip()
    if not protein_name and not fn_name:
        return None
    return f"{protein_name}||{fn_name}".lower()


def _categorize_pathway_assignment(
    pw_id: Optional[int],
    preexisting_ids: set,
    seen_new_ids: set,
) -> str:
    """Classify a pathway assignment for counter bookkeeping.

    Pure function — used by the LLM-batch loop in ``quick_assign_claims`` to
    decide which counter a pathway assignment belongs to, without relying on
    ``list.__contains__`` over ``all_pathways`` (which previously caused a
    double-count bug when two claims in the same batch hit the same
    just-created pathway).

    Returns one of:
        - ``"failed"`` — ``pw_id`` is None (pathway could not be resolved)
        - ``"preexisting"`` — pathway existed before the LLM pass started
        - ``"new"`` — newly created in this pass, first time seen
        - ``"repeat-new"`` — created earlier in this same pass; don't recount
    """
    if pw_id is None:
        return "failed"
    if pw_id in preexisting_ids:
        return "preexisting"
    if pw_id in seen_new_ids:
        return "repeat-new"
    return "new"


# Thread-local accumulator for write-time pathway drift entries. Used by
# ``_check_pathway_drift_at_write`` to record every drift it sees during
# a single ``quick_assign_pathways`` invocation, then flushed back into
# the result dict so runner.py can surface it via the pipeline
# diagnostics sidecar (frontend reads ``_diagnostics.pathway_drifts``).
import threading as _threading
_pathway_drift_state = _threading.local()


def _drift_collector() -> Optional[List[Dict[str, Any]]]:
    """Return the per-thread drift accumulator, or None if not active."""
    return getattr(_pathway_drift_state, "collector", None)


def _begin_drift_collection() -> None:
    """Start a fresh drift accumulator on the current thread."""
    _pathway_drift_state.collector = []


def _end_drift_collection() -> List[Dict[str, Any]]:
    """Return accumulated drifts and clear the per-thread collector."""
    drifts = getattr(_pathway_drift_state, "collector", None) or []
    _pathway_drift_state.collector = None
    return list(drifts)


def _record_drift(
    *,
    interactor: str,
    function: str,
    from_pathway: str,
    from_score: int,
    to_pathway: str,
    to_score: int,
    action: str,
    interaction_id: Any,
) -> None:
    """Append a drift entry to the active collector (no-op if inactive)."""
    coll = _drift_collector()
    if coll is None:
        return
    coll.append({
        "interactor": interactor or "?",
        "function": function or "?",
        "from": from_pathway or "?",
        "from_score": int(from_score or 0),
        "to": to_pathway or "?",
        "to_score": int(to_score or 0),
        "action": action,  # "corrected" or "report-only"
        "interaction_id": interaction_id,
    })


def _check_pathway_drift_at_write(claim: Any, proposed_pw: Any) -> Any:
    """Run the pathway-content validator at write time and reassign on drift.

    P3.1: this is the canonical home for pathway drift correction (the
    write-time fix). Previously, drift was detected at read time in
    ``services/data_builder.py`` with ``PATHWAY_AUTO_CORRECT=false``, so
    drifted claims were logged but not corrected. The DB read kept
    returning the drifted assignment until a future quick_assign run
    happened to choose differently.

    Runs the seed-keyword pathway validator on this claim's prose
    (mechanism, effect description, biological consequences,
    specific effects, function name). When the validator says the
    proposed pathway has a strictly lower keyword score than another
    pathway by a meaningful gap (>=2 hits AND >= 2× ratio per
    ``classify_pathway``), we look up the implied pathway in the DB
    and return that one for assignment instead.

    Skipped (returns proposed_pw unchanged) when:
      - claim is chain-derived (chain context dominates pathway choice)
      - implied pathway is not in DB (can't reassign without it)
      - PATHWAY_DRIFT_WRITE_TIME=false in env (operator opt-out)
      - validator import fails (degrade gracefully)
    """
    if proposed_pw is None or getattr(proposed_pw, "id", None) is None:
        return proposed_pw
    if os.getenv("PATHWAY_DRIFT_WRITE_TIME", "true").lower() == "false":
        return proposed_pw

    fn_ctx = (getattr(claim, "function_context", None) or "").strip().lower()
    if fn_ctx == "chain_derived":
        # Chain-derived claims pull pathway from the chain's dominant
        # vote — keyword-on-prose drift is meaningless here because the
        # prose includes upstream cascade context (per F6 in
        # utils/pathway_content_validator.classify_pathway).
        return proposed_pw

    try:
        from utils.pathway_content_validator import classify_pathway
    except Exception:  # noqa: BLE001 — degrade gracefully on any import error
        return proposed_pw

    biological_consequences = getattr(claim, "biological_consequences", None) or []
    specific_effects = getattr(claim, "specific_effects", None) or []

    claim_dict = {
        "function": getattr(claim, "function_name", None) or "",
        "cellular_process": getattr(claim, "mechanism", None) or "",
        "effect_description": getattr(claim, "effect_description", None) or "",
        "biological_consequence": list(biological_consequences),
        "specific_effects": list(specific_effects),
        "function_context": getattr(claim, "function_context", None),
        "pathway": getattr(proposed_pw, "name", None) or "",
    }

    try:
        verdict = classify_pathway(claim_dict)
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            f"  [PATHWAY DRIFT WRITE-TIME] validator crashed for "
            f"interaction_id={getattr(claim, 'interaction_id', '?')}: "
            f"{type(exc).__name__}: {exc}; keeping proposed pathway."
        )
        return proposed_pw

    if verdict.reason != "drift":
        return proposed_pw

    try:
        from models import Pathway
        implied_pw = Pathway.query.filter_by(name=verdict.implied).first()
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            f"  [PATHWAY DRIFT WRITE-TIME] DB lookup of implied pathway "
            f"{verdict.implied!r} failed: {type(exc).__name__}: {exc}; "
            f"keeping proposed pathway."
        )
        return proposed_pw

    # Resolve a partner symbol for the diagnostics surface so the
    # frontend can attach the badge to the correct interactor node.
    partner_symbol = None
    try:
        inter = getattr(claim, "interaction", None)
        if inter is not None:
            pa = getattr(inter, "protein_a", None)
            pb = getattr(inter, "protein_b", None)
            partner_symbol = (
                getattr(pa, "symbol", None)
                or getattr(pb, "symbol", None)
            )
    except Exception:
        partner_symbol = None

    if implied_pw is None or implied_pw.id is None:
        # Implied pathway doesn't exist in DB — log the drift but stick
        # with the proposed pathway (creating new pathways is the LLM's
        # job, not the validator's). Record as report-only so the
        # frontend banner can still surface it.
        logger.info(
            f"  [PATHWAY DRIFT WRITE-TIME] interaction_id="
            f"{getattr(claim, 'interaction_id', '?')} "
            f"function={getattr(claim, 'function_name', None)!r}: "
            f"drift detected (assigned={verdict.assigned!r} "
            f"score={verdict.assigned_score}, implied={verdict.implied!r} "
            f"score={verdict.top_alternative_score}) but implied pathway "
            "not in DB; keeping proposed."
        )
        _record_drift(
            interactor=partner_symbol or getattr(claim, "function_name", None) or "?",
            function=getattr(claim, "function_name", None) or "?",
            from_pathway=verdict.assigned,
            from_score=verdict.assigned_score,
            to_pathway=verdict.implied,
            to_score=verdict.top_alternative_score,
            action="report-only",
            interaction_id=getattr(claim, "interaction_id", None),
        )
        return proposed_pw

    logger.info(
        f"  [PATHWAY DRIFT CORRECTED] interaction_id="
        f"{getattr(claim, 'interaction_id', '?')} "
        f"function={getattr(claim, 'function_name', None)!r}: "
        f"reassigning {verdict.assigned!r} (score {verdict.assigned_score}) "
        f"→ {verdict.implied!r} (score {verdict.top_alternative_score}) "
        "via write-time prose keyword analysis."
    )
    _record_drift(
        interactor=partner_symbol or getattr(claim, "function_name", None) or "?",
        function=getattr(claim, "function_name", None) or "?",
        from_pathway=verdict.assigned,
        from_score=verdict.assigned_score,
        to_pathway=verdict.implied,
        to_score=verdict.top_alternative_score,
        action="corrected",
        interaction_id=getattr(claim, "interaction_id", None),
    )
    return implied_pw


def _apply_llm_pathway_to_claim(
    claim: Any,
    pw: Any,
    *,
    preexisting_ids: set,
    seen_new_ids: set,
    all_pathways: list,
    processed_ids: List[int],
    counters: Dict[str, int],
) -> str:
    """Apply an LLM-resolved pathway to one claim, updating all counters.

    Shared by the chain-group LLM path and the standalone LLM path — each
    had copy/pasted bookkeeping before this extraction. Mutates ``claim``,
    ``seen_new_ids``, ``all_pathways``, ``processed_ids``, and ``counters``
    in place; returns the category from ``_categorize_pathway_assignment``.

    ``counters`` must have ``matched_existing``, ``created_new``, ``failed``
    keys. The "repeat-new" category leaves counters alone by design so a
    chain of N claims that resolves to one newly-created pathway counts as
    exactly 1 ``created_new`` (not N).

    P3.1 write-time drift correction: before assignment we run
    ``_check_pathway_drift_at_write`` which compares the proposed pathway
    against prose keywords. When the prose strongly favors a different
    DB-resident pathway, we substitute the implied pathway and recategorize
    so the counters reflect the corrected assignment.
    """
    # P3.1: write-time pathway drift correction. May return a different
    # Pathway row when the proposed one disagrees with the prose.
    pw = _check_pathway_drift_at_write(claim, pw)

    pw_id = getattr(pw, "id", None) if pw else None
    category = _categorize_pathway_assignment(pw_id, preexisting_ids, seen_new_ids)
    claim_interaction_id = (
        getattr(claim, "interaction_id", None)
        or getattr(getattr(claim, "interaction", None), "id", None)
        or "<unknown>"
    )

    if category == "failed":
        # Silent-failure surfacing: log which claim failed and why so the
        # run summary's "N failed" line is diagnosable instead of opaque.
        logger.warning(
            f"  [CLAIM FAILED] interaction_id={claim_interaction_id} "
            f"function={claim.function_name!r} "
            f"current_pathway_id={claim.pathway_id}: "
            f"LLM returned pathway={pw!r}, category=failed "
            "(pw is None or categorization rejected)"
        )
        counters["failed"] += 1
        return category

    # pw is non-None and has an id by construction.
    was_unassigned = claim.pathway_id is None
    # H1: safe-update skips the assignment if another claim on the same
    # interaction already carries (function_name, pw.name, function_context).
    # When skipped, fall through to "failed" bookkeeping — leave the claim
    # for step7 / manual cleanup.
    if not _assign_claim_pathway_safe(claim, pw):
        # Silent-failure surfacing: which claim collided with which pathway
        # on the uq_claim_interaction_fn_pw_ctx unique index.
        logger.warning(
            f"  [CLAIM FAILED] interaction_id={claim_interaction_id} "
            f"function={claim.function_name!r} "
            f"target_pathway={getattr(pw, 'name', None)!r}: "
            "safe-update skipped — another claim on this interaction "
            "already owns (function_name, pathway, function_context); "
            "leaving for step7 / manual review"
        )
        counters["failed"] += 1
        return "failed"
    if was_unassigned:
        # Idempotent: re-running the LLM batch (e.g. after a partial
        # failure) must not double-count popularity.
        pw.usage_count = (pw.usage_count or 0) + 1

    if category == "preexisting":
        counters["matched_existing"] += 1
    elif category == "new":
        seen_new_ids.add(pw.id)
        if pw not in all_pathways:
            all_pathways.append(pw)
        counters["created_new"] += 1
    else:
        # "repeat-new": this claim shares a just-created pathway with a
        # sibling chain claim, so popularity is already bumped. Track
        # it separately so the run summary line adds up to the total
        # (F8 — previously these claims were silently uncounted, making
        # the user think 6 of 158 had vanished).
        counters["repeat_new"] = counters.get("repeat_new", 0) + 1

    processed_ids.append(claim.id)
    _sync_claim_to_interaction_data(claim, pw.name)
    return category


def _build_chain_display(members: List[Any]) -> str:
    """Human-readable chain header for ``CHAIN_BATCH_ASSIGN_PROMPT``.

    Returns something like ``"ATXN3 → FOXO4 → SOD2"`` by pulling the
    first chain member's mediator chain or, as a fallback, listing the
    distinct protein_a/protein_b symbols across all members. Purely
    cosmetic — the LLM sees it to understand what "the chain" refers to.
    """
    for m in members:
        interaction = getattr(m, "interaction", None)
        data = getattr(interaction, "data", None) if interaction else None
        if isinstance(data, dict):
            chain_ctx = data.get("chain_context") or {}
            full_chain = chain_ctx.get("full_chain") or []
            if full_chain:
                return " → ".join(str(p) for p in full_chain)
            mediator_chain = data.get("mediator_chain") or []
            if mediator_chain:
                a = interaction.protein_a.symbol if interaction.protein_a else "?"
                b = interaction.protein_b.symbol if interaction.protein_b else "?"
                proteins = [a, *[str(p) for p in mediator_chain], b]
                deduped = []
                for protein in proteins:
                    if protein == "?":
                        continue
                    if deduped and deduped[-1] == protein:
                        continue
                    deduped.append(protein)
                if len(deduped) >= 2:
                    return " → ".join(deduped)
            # No canonical chain — render the endpoint pair only. The
            # old fallback assumed [a, *mediator, b] with protein_a at
            # head, which inverted biology for chains where the query
            # sits elsewhere. Display the two known endpoints instead
            # of inventing a direction.
            a = interaction.protein_a.symbol if interaction.protein_a else "?"
            b = interaction.protein_b.symbol if interaction.protein_b else "?"
            if a != "?" and b != "?":
                return f"{a} → {b}"
    # Fallback: enumerate distinct proteins seen across the group.
    proteins: List[str] = []
    seen: set = set()
    for m in members:
        interaction = getattr(m, "interaction", None)
        if not interaction:
            continue
        for p in (interaction.protein_a, interaction.protein_b):
            if p and p.symbol and p.symbol not in seen:
                seen.add(p.symbol)
                proteins.append(p.symbol)
    return " → ".join(proteins) if proteins else "<unknown chain>"


# ---------------------------------------------------------------------------
# Claim-Level Assignment (New)
# ---------------------------------------------------------------------------

def quick_assign_claims(
    interaction_ids: Optional[List[int]] = None,
) -> Dict[str, Any]:
    """Assign pathways to individual InteractionClaims using hierarchy-aware LLM batching.

    Chain-aware: claims sharing a ``_chain_group`` key (in interaction data or
    claim data) are grouped and assigned to a single shared pathway, ensuring
    all links in a biological chain (e.g. ATXN3->VCP->RNF8) stay together.
    Standalone claims are still assigned independently.

    PathwayInteraction records are synced afterwards.
    """
    try:
        from app import app, db
        from models import (
            Interaction, InteractionClaim, IndirectChain,
            Pathway, PathwayParent, PathwayInteraction,
        )
    except ImportError as e:
        logger.error(f"Failed to import app/db: {e}")
        return {"total": 0, "matched_existing": 0, "created_new": 0, "failed": 0, "processed_claim_ids": []}

    with app.app_context():
        # 0. Fast idempotency short-circuit BEFORE any expensive setup.
        # When re-querying a protein whose claims are already fully
        # assigned, everything below is a no-op — but the hierarchy
        # serialization (~20K tokens) and context-cache API call are
        # not free. Bail early with just two cheap COUNT queries.
        from models import InteractionClaim as _ICEarly, PathwayInteraction as _PIEarly
        _scope_q = _ICEarly.query
        if interaction_ids:
            _scope_q = _scope_q.filter(_ICEarly.interaction_id.in_(interaction_ids))
        _total = _scope_q.count()
        _unassigned_count = _scope_q.filter(_ICEarly.pathway_id.is_(None)).count()
        if _total > 0 and _unassigned_count == 0:
            # Claims all assigned. Run the cheap in-place syncs so
            # PathwayInteractions stay aligned (zero-delta if already
            # in sync) and return.
            if _chain_unify_enabled():
                from models import IndirectChain as _ICh, Pathway as _PwE
                _unify_all_chain_claims(db, _ICh, _ICEarly, _PwE, interaction_ids)
            _sync_pathway_interactions(db, _ICEarly, _PIEarly, interaction_ids)
            _sync_interaction_finalized_pathway(db, _ICEarly, interaction_ids)
            db.session.commit()
            logger.info(
                f"Quick assign claims: idempotent no-op — "
                f"{_total} claim(s) already fully assigned"
            )
            return {
                "total": _total,
                "matched_existing": _total,
                "created_new": 0,
                "failed": 0,
                "processed_claim_ids": [],
                "skipped_noop": True,
            }

        # 1. Serialize full hierarchy once
        hierarchy_text = _serialize_pathway_hierarchy(db, Pathway, PathwayParent)

        # Try to create a Gemini context cache for the hierarchy
        # (avoids sending ~5K-20K tokens of hierarchy in every LLM call)
        _hierarchy_cache_name = None
        try:
            from utils.gemini_runtime import create_or_get_system_cache
            hierarchy_system = (
                "You are a molecular biology expert. Below is the COMPLETE pathway "
                "hierarchy in the database. When asked to assign a claim to a pathway, "
                "you MUST prefer the most specific LEAF pathway that matches the claim's "
                "mechanism. A higher keyword_score in the per-call candidate list means "
                "the candidate is a closer mechanistic match — always pick that one over "
                "a lower-scoring broader parent. Never pick a parent when a matching "
                "child is in the candidate list.\n\nPATHWAY HIERARCHY:\n"
                + hierarchy_text[:20000]
            )
            # Shorter TTL so a new pathway created mid-run doesn't leave a
            # stale hierarchy snapshot in the cache for an hour — 300s
            # gives fast enough refresh and we also invalidate on every
            # _resolve_or_create_pathway success.
            _hierarchy_cache_name = create_or_get_system_cache(
                system_text=hierarchy_system,
                model="gemini-3-flash-preview",
                ttl_seconds=300,
                display_name="pathway-hierarchy-cache",
            )
            _register_hierarchy_cache(_hierarchy_cache_name)
            logger.info("Created/retrieved hierarchy context cache: %s", _hierarchy_cache_name)
        except Exception as e:
            logger.info("Context caching unavailable (%s), using inline hierarchy", e)
            _hierarchy_cache_name = None
            _register_hierarchy_cache(None)

        # 2. Load all pathways for fuzzy matching (most specific first)
        all_pathways = Pathway.query.filter(
            Pathway.hierarchy_level >= 0
        ).order_by(Pathway.hierarchy_level.desc()).all()

        # 3. Query unassigned claims
        query = InteractionClaim.query.filter(InteractionClaim.pathway_id.is_(None))
        if interaction_ids:
            query = query.filter(InteractionClaim.interaction_id.in_(interaction_ids))

        # Eagerly load interaction + proteins
        from sqlalchemy.orm import joinedload
        unassigned = query.options(
            joinedload(InteractionClaim.interaction).joinedload(Interaction.protein_a),
            joinedload(InteractionClaim.interaction).joinedload(Interaction.protein_b),
        ).all()

        if not unassigned:
            logger.info("Quick assign claims: all claims already have pathway assignments")
            # Pre-assigned path: only run the chain unifier when explicitly
            # opted in. By default leave per-hop and per-claim pathway
            # diversity intact — the old default was to flatten every hop
            # in a chain to the same pathway, which collapsed legitimate
            # cross-pathway cascades into one label.
            if _chain_unify_enabled():
                _unify_all_chain_claims(db, IndirectChain, InteractionClaim, Pathway, interaction_ids)
            else:
                logger.info("  [CHAIN UNIFY] Skipped — CHAIN_PATHWAY_UNIFY=false explicitly disables selective chain unification (P3.1)")
            _sync_pathway_interactions(db, InteractionClaim, PathwayInteraction, interaction_ids)
            _sync_interaction_finalized_pathway(db, InteractionClaim, interaction_ids)
            db.session.commit()
            return {
                "total": 0,
                "matched_existing": 0,
                "created_new": 0,
                "failed": 0,
                "processed_claim_ids": [],
            }

        logger.info(f"Quick assign claims: {len(unassigned)} claims need pathway assignment")

        matched_existing = 0
        created_new = 0
        failed = 0
        processed_ids: List[int] = []
        # Track pathways we've already counted as "new" in this LLM pass, so a
        # second claim assigned to the same just-created pathway doesn't
        # double-count (previous bug: `pw not in all_pathways` identity check
        # would flip to matched_existing on the second claim).
        seen_new_ids: set = set()

        # ── 4. Tier 1+2: DB matching ─────────────────────────────
        # Try exact / fuzzy DB match before any LLM call. Claims that hit
        # are assigned immediately; the rest fall through to Tier 3 batching.
        standalone_llm_todo: List[Any] = []

        for claim in unassigned:
            matched = _try_db_match(claim, all_pathways)
            if matched:
                # Only bump usage_count on a first-time assignment — on retry
                # the claim already has a pathway_id, so re-running the
                # assignment shouldn't inflate popularity metrics.
                was_unassigned = claim.pathway_id is None
                # H1: safe-update skips assignments that would collide
                # against the uq_claim_interaction_fn_pw_ctx index.
                if not _assign_claim_pathway_safe(claim, matched):
                    # Fall through to the LLM path; it may pick a
                    # different pathway that doesn't collide.
                    standalone_llm_todo.append(claim)
                    continue
                if was_unassigned:
                    matched.usage_count = (matched.usage_count or 0) + 1
                matched_existing += 1
                processed_ids.append(claim.id)
                _sync_claim_to_interaction_data(claim, matched.name)
            else:
                standalone_llm_todo.append(claim)

        # Snapshot pathway ids that existed before the LLM pass started, so we
        # can tell "LLM said new but we matched a pre-existing pathway" apart
        # from "LLM said new and we really did create it".
        preexisting_ids: set = {p.id for p in all_pathways if getattr(p, "id", None) is not None}
        counters: Dict[str, int] = {
            "matched_existing": matched_existing,
            "created_new": created_new,
            "failed": failed,
        }

        # ── 5a. Partition Tier-3 work into chain groups + standalones ──
        # Claims that share a ``_chain_group`` describe links in the same
        # indirect chain and MUST end up on one pathway. Route them through
        # ``CHAIN_BATCH_ASSIGN_PROMPT`` (one LLM call per chain, one pathway
        # decision shared across every member). Everything else goes through
        # the normal batched prompt.
        chain_groups_todo: Dict[str, List[Any]] = {}
        truly_standalone_todo: List[Any] = []
        for claim in standalone_llm_todo:
            chain_group = _extract_chain_group(claim)
            if chain_group:
                chain_groups_todo.setdefault(chain_group, []).append(claim)
            else:
                truly_standalone_todo.append(claim)

        if standalone_llm_todo and hierarchy_text:
            from scripts.pathway_v2.llm_utils import _call_gemini_json

            # ── 5b. Chain-group LLM path ─────────────────────────
            # One LLM call per chain group. The prompt says "ALL claims
            # below MUST be assigned to the SAME single pathway" so the
            # model picks one decision for the whole chain.
            for chain_key, chain_members in chain_groups_todo.items():
                chain_claims_json = json.dumps(
                    [_build_claim_info(c) for c in chain_members], indent=2,
                )
                chain_display = _build_chain_display(chain_members)

                # Re-read the module-global cache name — it may have been
                # cleared by a new pathway's _invalidate_hierarchy_cache()
                # in an earlier iteration, in which case we fall back to
                # inline hierarchy so the stale cached system prompt
                # doesn't steer the LLM at an old tree.
                _hierarchy_cache_name = _ACTIVE_HIERARCHY_CACHE_NAME
                if _hierarchy_cache_name:
                    _chain_ranked_c = _score_pathway_candidates(
                        _combined_claim_text(chain_members), all_pathways,
                    )
                    _chain_cand_block = _format_candidate_block(_chain_ranked_c)
                    chain_prompt = (
                        "The following scientific claims are links in the SAME "
                        "biological chain. ALL claims below MUST be assigned to "
                        "the SAME single pathway from the hierarchy in the "
                        "system context.\n\n"
                        f"Chain: {chain_display}\n\n"
                        "TOP PRE-SCORED CANDIDATES (higher keyword_score = deeper/more specific match):\n"
                        f"{_chain_cand_block}\n\n"
                        f"CLAIMS:\n{chain_claims_json}\n\n"
                        "STRICT RULES:\n"
                        "1. ALL claims receive the SAME pathway — they are mechanistically linked in one chain.\n"
                        "2. You MUST pick from the numbered CANDIDATES unless NONE semantically fits.\n"
                        "3. When two candidates match, ALWAYS pick the HIGHER keyword_score — "
                        "higher score = deeper/more specific; lower score = broader parent. "
                        "Picking the parent over a well-scored child is INCORRECT.\n"
                        "4. NEVER pick a candidate with keyword_score=0 when one with score ≥ 2 exists.\n"
                        "5. Only create a NEW pathway if the top candidate scores ≤ 1 AND nothing fits.\n"
                        "Respond with JSON matching the schema exactly."
                    )
                else:
                    _chain_ranked = _score_pathway_candidates(
                        _combined_claim_text(chain_members), all_pathways,
                    )
                    chain_prompt = CHAIN_BATCH_ASSIGN_PROMPT.format(
                        hierarchy=hierarchy_text[:20000],
                        chain_display=chain_display,
                        claims_json=chain_claims_json,
                        candidate_block=_format_candidate_block(_chain_ranked),
                    )

                try:
                    chain_resp = _call_gemini_json(
                        chain_prompt,
                        response_json_schema=CHAIN_BATCH_ASSIGN_SCHEMA,
                        model="gemini-3-flash-preview",
                        thinking_level="low",
                        disable_afc=True,
                        max_output_tokens=LLM_CHAIN_BATCH_MAX_OUTPUT_TOKENS,
                        cached_content=_hierarchy_cache_name,
                    )
                except Exception as e:
                    logger.warning(
                        f"Chain LLM claim assign failed for chain "
                        f"'{chain_key}' ({len(chain_members)} claims): "
                        f"{type(e).__name__}: {e}",
                        exc_info=True,
                    )
                    for m in chain_members:
                        m.context_data = {**(m.context_data or {}), "_pathway_assignment": "llm_error"}
                    counters["failed"] += len(chain_members)
                    continue

                if not isinstance(chain_resp, dict) or not chain_resp.get("pathway_name"):
                    logger.warning(
                        f"Chain LLM claim assign returned malformed response "
                        f"(type={type(chain_resp).__name__}) for chain "
                        f"'{chain_key}'; counting all {len(chain_members)} "
                        f"claims as failed"
                    )
                    for m in chain_members:
                        m.context_data = {**(m.context_data or {}), "_pathway_assignment": "malformed_response"}
                    counters["failed"] += len(chain_members)
                    continue

                chain_pw = _resolve_or_create_pathway(
                    pathway_name=(chain_resp.get("pathway_name") or "").strip(),
                    is_new=chain_resp.get("is_new", False),
                    parent_name=(chain_resp.get("parent_pathway") or "").strip(),
                    hierarchy_level=chain_resp.get("hierarchy_level"),
                    all_pathways=all_pathways,
                    db=db,
                    Pathway=Pathway,
                    PathwayParent=PathwayParent,
                )

                if chain_pw is None or getattr(chain_pw, "id", None) is None:
                    logger.warning(
                        f"Chain LLM decision for '{chain_key}' did not "
                        f"resolve to a pathway; counting {len(chain_members)} "
                        f"claims as failed"
                    )
                    for m in chain_members:
                        m.context_data = {**(m.context_data or {}), "_pathway_assignment": "resolve_failed"}
                    counters["failed"] += len(chain_members)
                    continue

                for member in chain_members:
                    _apply_llm_pathway_to_claim(
                        member,
                        chain_pw,
                        preexisting_ids=preexisting_ids,
                        seen_new_ids=seen_new_ids,
                        all_pathways=all_pathways,
                        processed_ids=processed_ids,
                        counters=counters,
                    )

            # ── 5c. Standalone LLM path ──────────────────────────
            # Claims with no chain group — treated independently, batched
            # ``LLM_BATCH_SIZE`` per call via ``BATCH_CLAIM_ASSIGN_PROMPT``.
            for i in range(0, len(truly_standalone_todo), LLM_BATCH_SIZE):
                batch = truly_standalone_todo[i:i + LLM_BATCH_SIZE]

                claims_json_str = json.dumps(
                    [_build_claim_info(c) for c in batch], indent=2,
                )

                # Re-read the module-global cache name (may be None if a
                # new pathway was just created — see the chain-loop
                # comment above for rationale).
                _hierarchy_cache_name = _ACTIVE_HIERARCHY_CACHE_NAME
                if _hierarchy_cache_name:
                    _batch_ranked_c = _score_pathway_candidates(
                        _combined_claim_text(batch), all_pathways,
                    )
                    _batch_cand_block = _format_candidate_block(_batch_ranked_c)
                    prompt = (
                        "For each scientific claim below, assign it to the SINGLE BEST "
                        "existing pathway from the hierarchy provided in the system context.\n"
                        "Use pathway_id when referencing an existing pathway.\n\n"
                        "TOP PRE-SCORED CANDIDATES for this batch (higher keyword_score = deeper/more specific match):\n"
                        f"{_batch_cand_block}\n\n"
                        f"CLAIMS:\n{claims_json_str}\n\n"
                        "STRICT RULES:\n"
                        "1. You MUST pick from the numbered CANDIDATES unless NONE semantically fits.\n"
                        "2. When two candidates match, ALWAYS pick the HIGHER keyword_score — "
                        "higher score = deeper/more specific; lower score = broader parent. "
                        "Picking the parent over a well-scored child is INCORRECT (e.g. "
                        "'Mitophagy' over 'Mitochondrial Quality Control' when the claim "
                        "mentions PINK1/PRKN, or 'Cell Cycle' over 'DNA Damage Response' "
                        "when the claim mentions cyclins/CDK1/checkpoints).\n"
                        "3. NEVER pick a candidate with keyword_score=0 when one with score ≥ 2 exists.\n"
                        "4. Only create a NEW pathway if the top candidate scores ≤ 1 AND nothing fits. "
                        "For new: provide parent_pathway and hierarchy_level.\n"
                        "Note: Chain-grouped claims are handled separately.\n"
                        "Respond with JSON matching the schema exactly."
                    )
                else:
                    _batch_ranked = _score_pathway_candidates(
                        _combined_claim_text(batch), all_pathways,
                    )
                    prompt = BATCH_CLAIM_ASSIGN_PROMPT.format(
                        hierarchy=hierarchy_text[:20000],
                        claims_json=claims_json_str,
                        candidate_block=_format_candidate_block(_batch_ranked),
                    )

                try:
                    resp = _call_gemini_json(
                        prompt,
                        response_json_schema=BATCH_CLAIM_ASSIGN_SCHEMA,
                        model="gemini-3-flash-preview",
                        thinking_level="low",
                        disable_afc=True,
                        max_output_tokens=LLM_BATCH_ASSIGN_MAX_OUTPUT_TOKENS,
                        cached_content=_hierarchy_cache_name,
                    )
                except Exception as e:
                    logger.warning(
                        f"Batch LLM claim assign failed for batch of "
                        f"{len(batch)} claims: {type(e).__name__}: {e}",
                        exc_info=True,
                    )
                    counters["failed"] += len(batch)
                    continue

                if not isinstance(resp, dict):
                    logger.warning(
                        f"Batch LLM claim assign returned non-dict "
                        f"(type={type(resp).__name__}) for batch of "
                        f"{len(batch)} claims; counting all as failed"
                    )
                    counters["failed"] += len(batch)
                    continue
                if "assignments" not in resp:
                    logger.warning(
                        f"Batch LLM claim assign missing 'assignments' key "
                        f"(keys={list(resp.keys())[:10]}) for batch of "
                        f"{len(batch)} claims; counting all as failed"
                    )
                    counters["failed"] += len(batch)
                    continue

                claim_by_id = {c.id: c for c in batch}

                for assignment in resp["assignments"]:
                    cid = assignment.get("claim_id")
                    claim = claim_by_id.get(cid)
                    if not claim:
                        logger.warning(f"LLM returned unknown claim_id={cid}, skipping")
                        continue

                    # The LLM handed us an assignment for this claim — it's no
                    # longer "unprocessed". Pop regardless of outcome so the
                    # post-loop `failed += len(claim_by_id)` only counts claims
                    # the LLM omitted entirely.
                    claim_by_id.pop(cid, None)

                    pw = _resolve_or_create_pathway(
                        pathway_name=(assignment.get("pathway_name") or "").strip(),
                        is_new=assignment.get("is_new", False),
                        parent_name=(assignment.get("parent_pathway") or "").strip(),
                        hierarchy_level=assignment.get("hierarchy_level"),
                        all_pathways=all_pathways,
                        db=db,
                        Pathway=Pathway,
                        PathwayParent=PathwayParent,
                        pathway_id=assignment.get("pathway_id"),
                    )

                    _apply_llm_pathway_to_claim(
                        claim,
                        pw,
                        preexisting_ids=preexisting_ids,
                        seen_new_ids=seen_new_ids,
                        all_pathways=all_pathways,
                        processed_ids=processed_ids,
                        counters=counters,
                    )

                # Count remaining unprocessed claims (LLM didn't return an
                # assignment for them at all) as failed.
                counters["failed"] += len(claim_by_id)

        # Copy counter dict back into locals so the existing trailing code
        # (consistency passes, logging, return) keeps working unchanged.
        matched_existing = counters["matched_existing"]
        created_new = counters["created_new"]
        failed = counters["failed"]
        repeat_new = counters.get("repeat_new", 0)

        # 5c. Protein-level pathway consistency pass
        # If the same protein's same function got different pathways across
        # different interactions (e.g. VCP in chain A vs chain B), unify them
        # to the most-assigned pathway. Preserves per-function independence
        # while ensuring one biological function = one pathway globally.
        _consistency_fixes = _apply_consistency_pass(
            claims=unassigned,
            group_key_fn=_protein_fn_group_key,
            dominant_picker=_pick_majority_pathway_id,
            Pathway_cls=Pathway,
            sync_fn=_sync_claim_to_interaction_data,
        )
        if _consistency_fixes:
            logger.info(
                f"  [PATHWAY CONSISTENCY] Unified {_consistency_fixes} claim(s) "
                f"where same protein+function had different pathways"
            )

        # 5d. Chain-level pathway consistency pass
        # Defense-in-depth: the chain-aware LLM batch above tries to put all
        # chain members on one pathway upfront, but if that path is skipped
        # (e.g. no chain cache, batch failure, chain-group tag missing) we
        # still want a belt-and-suspenders unification here. Prefers the
        # net-effect claim's pathway (function_context == "net") because it
        # describes the biological endpoint of the full chain.
        if _chain_unify_enabled():
            _chain_fixes = _apply_consistency_pass(
                claims=unassigned,
                group_key_fn=_extract_chain_group,
                dominant_picker=_pick_chain_dominant_pathway_id,
                Pathway_cls=Pathway,
                sync_fn=_sync_claim_to_interaction_data,
            )
            if _chain_fixes:
                logger.info(
                    f"  [CHAIN CONSISTENCY] Unified {_chain_fixes} claim(s) "
                    f"so every link in each chain shares the chain's dominant pathway"
                )
        else:
            logger.info("  [CHAIN CONSISTENCY] Skipped — per-hop pathway diversity preserved")

        # 6. Unify every chain's claims to one pathway FIRST.
        # Gated off by default now: when a cascade legitimately crosses
        # pathway boundaries (e.g., query → kinase pathway → autophagy →
        # proteostasis), forcing a single label erases that biology.
        if _chain_unify_enabled():
            _unify_all_chain_claims(db, IndirectChain, InteractionClaim, Pathway, interaction_ids)
        else:
            logger.info("  [CHAIN UNIFY] Skipped — CHAIN_PATHWAY_UNIFY not enabled; per-hop pathways preserved")

        # 7. Sync PathwayInteraction table — reads final pathway_ids
        # (post-unification) so junction records match the unified state.
        _sync_pathway_interactions(db, InteractionClaim, PathwayInteraction, interaction_ids)

        # 8. Sync step3_finalized_pathway in JSONB from dominant claim pathway.
        # Also reads final pathway_ids so the JSONB matches card view.
        _sync_interaction_finalized_pathway(db, InteractionClaim, interaction_ids)

        db.session.commit()

        logger.info(
            f"Quick assign claims complete: {matched_existing} matched existing, "
            f"{created_new} created new, {repeat_new} repeat-new (chain mates), "
            f"{failed} failed (out of {len(unassigned)} total; "
            f"sum={matched_existing + created_new + repeat_new + failed})"
        )

        return {
            "total": len(unassigned),
            "matched_existing": matched_existing,
            "created_new": created_new,
            "failed": failed,
            "processed_claim_ids": processed_ids,
        }


def _sync_claim_to_interaction_data(claim, pathway_name: str) -> None:
    """Keep interaction.data['functions'][i]['pathway'] in sync with claim assignment."""
    interaction = claim.interaction
    if not interaction:
        return
    data = dict(interaction.data or {})
    for fn in data.get("functions", []):
        if isinstance(fn, dict) and fn.get("function") == claim.function_name:
            fn["pathway"] = pathway_name
    interaction.data = data


def _unify_all_chain_claims(
    db,
    IndirectChain,
    InteractionClaim,
    Pathway,
    interaction_ids=None,
    chain_ids=None,
) -> None:
    """Unify every IndirectChain's claims to a single dominant pathway.

    Runs AFTER pathway assignment so every claim already has its final
    ``pathway_id``. Walks every ``IndirectChain`` row in scope, loads
    ALL child claims (not just unassigned — that was the bug in the old
    ``_apply_consistency_pass`` call), picks the dominant pathway, and
    unifies every claim in the chain to it. When two claims under the
    same chain share ``(interaction_id, function_name)`` after unification,
    the richer one survives and the donor is merged + deleted.

    Scoping precedence (most specific wins):
      - ``chain_ids`` — direct chain filter. Use this when you want to
        touch EXACTLY these chains and nothing else. Required by the
        step7-repair caller so a single chain repair doesn't accidentally
        flatten unrelated chains that happen to share an interaction.
      - ``interaction_ids`` — derive the chain set from any chain whose
        origin interaction OR whose claims' interactions overlap the
        set. Used by the main ``quick_assign_claims`` path where the
        caller wants "everything touched by this query".
      - neither — all chains in the DB (unused in practice).

    Ties on pathway count are broken by ``Pathway.hierarchy_level``
    (most-specific wins) so "Alternative Splicing" beats "RNA Splicing"
    when both have equal claim counts.

    This is the ONLY chain-pathway enforcement pass in the codebase;
    ``db_sync`` used to run a duplicate pass but it ran before
    ``step3_finalized_pathway`` existed on any interaction, so it was
    structurally dead. That dead pass has been deleted.
    """
    from collections import Counter, defaultdict

    chain_query = IndirectChain.query
    if chain_ids:
        chain_query = chain_query.filter(IndirectChain.id.in_(list(chain_ids)))
    elif interaction_ids:
        # Scope to chains whose origin interaction OR any participant
        # interaction is in the queried set. Materialising the inner
        # chain_id lookup to a Python set keeps the query portable
        # across PostgreSQL and SQLite and avoids the subquery-vs-select
        # ambiguity in ``Column.in_()``.
        scope_ids = set(int(i) for i in interaction_ids)
        chain_ids_in_scope = {
            row[0]
            for row in (
                db.session.query(InteractionClaim.chain_id)
                .filter(InteractionClaim.interaction_id.in_(scope_ids))
                .filter(InteractionClaim.chain_id.isnot(None))
                .distinct()
                .all()
            )
        }
        from sqlalchemy import or_ as sqla_or
        chain_query = chain_query.filter(
            sqla_or(
                IndirectChain.origin_interaction_id.in_(scope_ids),
                IndirectChain.id.in_(chain_ids_in_scope),
            )
        )

    chains = chain_query.all()
    if not chains:
        return

    # Pre-load pathway hierarchy levels for tie-breaking.
    pw_levels: Dict[str, int] = {}
    for pw in Pathway.query.with_entities(Pathway.name, Pathway.hierarchy_level).all():
        pw_levels[pw.name] = pw.hierarchy_level or 0

    total_fixes = 0
    for chain in chains:
        chain_claims = (
            InteractionClaim.query
            .filter_by(chain_id=chain.id)
            .all()
        )
        if not chain_claims:
            continue

        # Pick dominant pathway. Order of preference:
        #   1. ``chain.pathway_name`` (set by db_sync via dominant-vote
        #      across the chain's full function set at write time) —
        #      authoritative when present, because it's derived once
        #      from the entire chain rather than from the per-link
        #      claim subset.
        #   2. claim majority — covers chains where pathway_name was
        #      never written on the chain row but individual claims
        #      have been pathway-assigned.
        #   3. give up.
        # The previous order let claim majority override the chain's
        # own pathway, which was the source of the "4th protein gets a
        # different pathway than the rest of the chain" bug — the
        # late-link claims often outnumber the early-link claims and
        # silently flipped the whole chain to a sibling pathway.
        pw_counts: Counter = Counter(
            c.pathway_name for c in chain_claims if c.pathway_name
        )
        if chain.pathway_name and (chain.pathway_name in pw_counts or not pw_counts):
            dominant_pw = chain.pathway_name
            # Visibility: if chain.pathway_name is strongly outvoted by
            # claim majority (chain row says A, but 5+ claims say B and
            # only 1 claim agrees with A), log it so a human can review
            # whether chain.pathway_name was set wrong at creation time.
            # We do NOT auto-override — chain.pathway_name is derived
            # from the full chain function set at write time and is
            # usually the more authoritative signal, but occasional
            # drift should be visible.
            if pw_counts:
                chain_vote = pw_counts.get(chain.pathway_name, 0)
                top_other_name, top_other_vote = next(
                    (
                        (n, c) for n, c in pw_counts.most_common()
                        if n != chain.pathway_name
                    ),
                    (None, 0),
                )
                if top_other_name and top_other_vote >= chain_vote + 3 and top_other_vote >= 4:
                    from utils.observability import log_event
                    log_event(
                        "chain_pathway_drift",
                        level="warn",
                        tag="CHAIN PATHWAY DRIFT",
                        chain_id=chain.id,
                        chain_pathway_name=chain.pathway_name,
                        chain_vote=chain_vote,
                        outvoting_pathway=top_other_name,
                        outvoting_vote=top_other_vote,
                        total_claims=len(chain_claims),
                    )
        elif pw_counts:
            max_count = pw_counts.most_common(1)[0][1]
            top_pathways = [name for name, cnt in pw_counts.items() if cnt == max_count]
            # P3.1 tie-break: explicit chain pathway → dominant chain
            # hop pathway → first-hop pathway → hierarchy_level. Codex
            # AI's review flagged that hierarchy-only tie-break is
            # biologically weird (a cascade may begin in one process
            # and culminate in another); first-hop biology is usually
            # the most authoritative when chain.pathway_name is unset.
            first_hop_pw = None
            try:
                proteins = list(chain.chain_proteins or [])
                if len(proteins) >= 2:
                    first_sig = f"{proteins[0]}->{proteins[1]}"
                    for c in chain_claims:
                        name = c.function_name or ""
                        if name.startswith("[") and "]" in name:
                            sig = name[1 : name.index("]")].strip()
                            if sig == first_sig and c.pathway_name:
                                first_hop_pw = c.pathway_name
                                break
            except Exception:
                first_hop_pw = None
            if first_hop_pw and first_hop_pw in top_pathways:
                dominant_pw = first_hop_pw
            else:
                dominant_pw = max(top_pathways, key=lambda n: pw_levels.get(n, 0))
        else:
            continue

        pw_row = Pathway.query.filter_by(name=dominant_pw).first()
        dominant_pw_id = pw_row.id if pw_row else None
        if not dominant_pw_id:
            continue

        # Group claims by (interaction_id, function_name) so we can
        # merge survivors when the unification would create duplicates
        # under the COALESCE unique index.
        groups: Dict[tuple, List] = defaultdict(list)
        for claim in chain_claims:
            groups[(claim.interaction_id, claim.function_name)].append(claim)

        for group in groups.values():
            if len(group) == 1:
                only = group[0]
                if only.pathway_name != dominant_pw:
                    only.pathway_name = dominant_pw
                    only.pathway_id = dominant_pw_id
                    total_fixes += 1
                continue

            # Pick survivor: prefer one already on target pathway,
            # else the one with the richest evidence.
            survivor = next(
                (c for c in group if c.pathway_name == dominant_pw),
                max(group, key=lambda c: len(c.evidence or [])),
            )

            for donor in group:
                if donor.id == survivor.id:
                    continue

                # Evidence — union by paper_title (lower-cased).
                surv_ev = list(survivor.evidence or [])
                surv_titles = {
                    (e.get("paper_title") or "").lower() for e in surv_ev
                }
                for ev in donor.evidence or []:
                    title = (ev.get("paper_title") or "").lower()
                    if title and title not in surv_titles:
                        surv_ev.append(ev)
                        surv_titles.add(title)
                survivor.evidence = surv_ev

                # PMIDs — union.
                surv_pmids = set(survivor.pmids or []) | set(donor.pmids or [])
                survivor.pmids = sorted(p for p in surv_pmids if p)

                # Prefer longer prose on mechanism / effect_description.
                for field in ("mechanism", "effect_description"):
                    donor_val = getattr(donor, field) or ""
                    surv_val = getattr(survivor, field) or ""
                    if len(donor_val) > len(surv_val):
                        setattr(survivor, field, donor_val)

                # Union JSONB arrays.
                for field in ("biological_consequences", "specific_effects"):
                    surv_arr = list(getattr(survivor, field) or [])
                    surv_strs = {str(x) for x in surv_arr}
                    for item in getattr(donor, field) or []:
                        if str(item) not in surv_strs:
                            surv_arr.append(item)
                            surv_strs.add(str(item))
                    setattr(survivor, field, surv_arr)

                # Keep higher confidence.
                donor_conf = float(donor.confidence or 0)
                surv_conf = float(survivor.confidence or 0)
                if donor_conf > surv_conf:
                    survivor.confidence = donor.confidence

                # Merge context_data (donor fills in missing keys only).
                if donor.context_data:
                    surv_ctx = dict(survivor.context_data or {})
                    for k, v in donor.context_data.items():
                        surv_ctx.setdefault(k, v)
                    survivor.context_data = surv_ctx

                # Keep richer raw_function_data.
                donor_raw = donor.raw_function_data or {}
                surv_raw = survivor.raw_function_data or {}
                if len(str(donor_raw)) > len(str(surv_raw)):
                    survivor.raw_function_data = donor_raw

                db.session.delete(donor)
                total_fixes += 1

            if survivor.pathway_name != dominant_pw:
                survivor.pathway_name = dominant_pw
                survivor.pathway_id = dominant_pw_id
                total_fixes += 1

        # Also update the chain record itself so callers of
        # ``GET /api/chain/<id>`` see the unified pathway.
        if chain.pathway_name != dominant_pw:
            chain.pathway_name = dominant_pw
            chain.pathway_id = dominant_pw_id

    if total_fixes:
        db.session.flush()
        logger.info(
            f"  [CHAIN UNIFY] Unified {total_fixes} chain claim(s) "
            f"across {len(chains)} chain(s) to one pathway each"
        )


def unify_one_chain_pathway(db, chain_id: int) -> int:
    """Single-chain unification entry point used by step7_repairs.

    Runs the same majority-vote logic as ``_unify_all_chain_claims`` but
    scoped to one chain. Returns the number of claim rows mutated. Raises
    if the chain doesn't exist or has no claims (caller treats that as a
    no-op to report).

    Exists so the step7 auto-fix path and the live pipeline path share
    exactly one implementation of chain unification — no drift between
    "checked mode" and "production mode".
    """
    from models import IndirectChain as _IC, InteractionClaim as _IClaim, Pathway as _Pw

    chain = _IC.query.get(int(chain_id))
    if chain is None:
        raise ValueError(f"IndirectChain {chain_id} does not exist")

    # Scope _unify_all_chain_claims to just this chain's interactions so
    # we don't accidentally touch unrelated chains. Collect the interaction
    # ids this chain's claims are attached to.
    interaction_ids = [
        r[0]
        for r in (
            db.session.query(_IClaim.interaction_id)
            .filter(_IClaim.chain_id == chain.id)
            .distinct()
            .all()
        )
    ]
    if not interaction_ids:
        return 0

    # Count current fragmentation so we can return a meaningful delta.
    before_pathways = (
        db.session.query(_IClaim.pathway_id)
        .filter(_IClaim.chain_id == chain.id)
        .distinct()
        .count()
    )

    # Scope DIRECTLY to this chain by id. The older path scoped by
    # interaction_ids, which pulled in every chain that shared an
    # interaction with this one — a common situation for chains with
    # shared middle proteins (e.g. TDP43→TP53→BAX and TDP43→TP53→CASP3
    # both touch TP53's interaction). Flattening bystanders is a silent
    # override of the user's pathway-diversity choice.
    _unify_all_chain_claims(db, _IC, _IClaim, _Pw, chain_ids=[chain.id])

    # Keep junction rows in sync with the new claim pathways so the
    # step7 re-check doesn't flag the stale PathwayInteractions that
    # unification just orphaned. Mirrors the in-pipeline path
    # quick_assign_claims (line ~1360) which calls _sync_pathway_interactions
    # right after _unify_all_chain_claims for exactly the same reason.
    from models import PathwayInteraction as _PI
    _sync_pathway_interactions(db, _IClaim, _PI, interaction_ids)
    db.session.flush()

    # After unification, all claims should share one pathway_id; the
    # number of mutated claims equals the number that weren't already
    # on the dominant pathway. We compute that by checking the current
    # state post-flush.
    after_pathways = (
        db.session.query(_IClaim.pathway_id)
        .filter(_IClaim.chain_id == chain.id)
        .distinct()
        .count()
    )
    # Rough signal of "did anything change": fragmentation collapsed
    # from N pathways to 1. Actual mutated-row count is tracked inside
    # _unify_all_chain_claims' counters and logged at INFO level.
    return max(0, before_pathways - after_pathways)


def _sync_interaction_finalized_pathway(db, InteractionClaim, interaction_ids=None):
    """Set each interaction's step3_finalized_pathway to its most-assigned claim pathway.

    This keeps the JSONB-level pathway consistent with the claim-level assignments
    so the data_builder's V2 pathway injection uses the right pathway name.
    """
    from collections import Counter

    query = InteractionClaim.query.filter(InteractionClaim.pathway_name.isnot(None))
    if interaction_ids:
        query = query.filter(InteractionClaim.interaction_id.in_(interaction_ids))

    # Group pathway_name counts by interaction_id
    ix_pw_counts: Dict[int, Counter] = {}
    ix_obj_cache: Dict[int, Any] = {}
    for claim in query.all():
        ix_pw_counts.setdefault(claim.interaction_id, Counter())[claim.pathway_name] += 1
        if claim.interaction_id not in ix_obj_cache and claim.interaction:
            ix_obj_cache[claim.interaction_id] = claim.interaction

    updated = 0
    for ix_id, counts in ix_pw_counts.items():
        dominant_pw = counts.most_common(1)[0][0]
        ix = ix_obj_cache.get(ix_id)
        if not ix:
            continue
        data = dict(ix.data or {})
        if data.get("step3_finalized_pathway") != dominant_pw:
            data["step3_finalized_pathway"] = dominant_pw
            ix.data = data
            updated += 1

    if updated:
        logger.info(f"  Synced step3_finalized_pathway on {updated} interaction(s)")


def _sync_pathway_interactions(db, InteractionClaim, PathwayInteraction, interaction_ids=None):
    """Rebuild PathwayInteraction records from current claim-level pathway assignments.

    Single source of truth: claims. A PathwayInteraction exists iff at least
    one InteractionClaim with the same (interaction_id, pathway_id) pair
    exists. Stale PIs (no backing claim) are deleted; missing PIs (claim
    exists but no junction row) are created.

    JSONB fields (interaction.data["functions"][].pathway,
    interaction.data["step3_finalized_pathway"]) are treated as a
    denormalized MIRROR of claims (maintained by
    ``_sync_claim_to_interaction_data``), never as an input source. Using
    them as input creates orphan PIs the moment a claim moves pathways
    (the old pathway's PI survives because the JSONB still mentions it),
    which is exactly the failure mode that produced the 11 stale-PI
    issues in the TDP43 verification log.
    """
    # Find all assigned claims (with pathway_id set)
    query = InteractionClaim.query.filter(InteractionClaim.pathway_id.isnot(None))
    if interaction_ids:
        query = query.filter(InteractionClaim.interaction_id.in_(interaction_ids))

    # Desired pairs from current claim assignments — THE only source
    desired_pairs = set()
    for claim in query.all():
        desired_pairs.add((claim.interaction_id, claim.pathway_id))

    # Current PathwayInteraction records
    pi_query = PathwayInteraction.query
    if interaction_ids:
        pi_query = pi_query.filter(PathwayInteraction.interaction_id.in_(interaction_ids))
    existing_pis = pi_query.all()
    existing_pairs = {(pi.interaction_id, pi.pathway_id): pi for pi in existing_pis}

    # Delete stale records (no longer backed by any claim)
    stale = set(existing_pairs.keys()) - desired_pairs
    deleted = 0
    for key in stale:
        pi = existing_pairs[key]
        db.session.delete(pi)
        deleted += 1

    # Create missing records
    missing = desired_pairs - set(existing_pairs.keys())
    created = 0
    for interaction_id, pathway_id in missing:
        pi = PathwayInteraction(
            pathway_id=pathway_id,
            interaction_id=interaction_id,
            assignment_method="quick_assign_claims",
            assignment_confidence=0.85,
        )
        db.session.add(pi)
        created += 1

    if created or deleted:
        logger.info(
            f"  Synced PathwayInteraction: +{created} created, -{deleted} stale removed "
            f"({len(desired_pairs)} total pairs)"
        )


# ---------------------------------------------------------------------------
# Public Entry Point
# ---------------------------------------------------------------------------

def quick_assign_pathways(
    interaction_ids: Optional[List[int]] = None,
) -> Dict[str, Any]:
    """Assign pathways — delegates to claim-level assignment.

    Returns dict with both processed_claim_ids and processed_interaction_ids
    for backward compatibility with run_pipeline.py, plus a
    ``pathway_drifts`` list of every write-time drift correction recorded
    by ``_check_pathway_drift_at_write`` (P3.1). The caller is expected
    to write that list into ``Logs/<protein>/pipeline_diagnostics.json``
    so the frontend can render the drift surface.
    """
    _begin_drift_collection()
    try:
        result = quick_assign_claims(interaction_ids=interaction_ids)
    finally:
        # Always flush the collector even if quick_assign_claims raised.
        result_drifts = _end_drift_collection()
    if isinstance(result, dict):
        result["pathway_drifts"] = result_drifts

    # Backward compat: callers (run_pipeline.py:118) expect "processed_interaction_ids"
    if "processed_claim_ids" in result and "processed_interaction_ids" not in result:
        try:
            from app import app, db
            from models import InteractionClaim
            claim_ids = result["processed_claim_ids"]
            if claim_ids:
                with app.app_context():
                    rows = db.session.query(InteractionClaim.interaction_id).filter(
                        InteractionClaim.id.in_(claim_ids)
                    ).distinct().all()
                    result["processed_interaction_ids"] = [r[0] for r in rows]
            else:
                result["processed_interaction_ids"] = list(interaction_ids or [])
        except Exception as exc:
            logger.warning(
                f"Failed to map processed_claim_ids → interaction_ids "
                f"(falling back to caller-provided ids): "
                f"{type(exc).__name__}: {exc}",
                exc_info=True,
            )
            result["processed_interaction_ids"] = list(interaction_ids or [])

    return result


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    result = quick_assign_pathways()
    print(f"Result: {result}")
