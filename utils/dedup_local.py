"""Local (non-LLM) function deduplication helpers.

This module holds the fast word-overlap / mechanism-overlap dedup logic
that previously lived inline in ``runner.py``. It is the fast-path
complement to the LLM-based dedup in ``utils/deduplicate_functions.py``:

- ``deduplicate_functions_local``: multi-pass in-place dedup of
  ``payload["ctx_json"]["interactors"][*]["functions"]``. Runs during
  iterative pipeline merging where LLM calls would be prohibitively
  expensive.
- ``dedup_words``, ``word_overlap``, ``is_mechanism_duplicate``: shared
  predicates also used by ``runner.py``'s merge logic.
- ``strip_empty_functions``: quality pass that removes functions without
  real mechanism/effect content.

These helpers are pure over their input payload except for in-place
mutation of ``interactor["functions"]`` (which the runner already relies
on). They never hit the network.
"""
from __future__ import annotations

import re
import sys
from typing import Any, Dict


# ---------------------------------------------------------------------------
# Word tokenization
# ---------------------------------------------------------------------------

_DEDUP_STOPWORDS = frozenset(
    "a an the and or of in on at to by for is it its with from as this that "
    "are was were be been has have had do does did not no but if so than can "
    "will may via into also each both between through during after before "
    "these those their them they which what when where how".split()
)


def dedup_words(text: str) -> set:
    """Extract meaningful word tokens for dedup comparison.

    Keeps biology terms (length >= 3, not in stopword list), lowercased.
    """
    tokens = re.findall(r"[A-Za-z0-9][\w-]*", text.lower())
    return {t for t in tokens if len(t) >= 3 and t not in _DEDUP_STOPWORDS}


def word_overlap(a: set, b: set) -> float:
    """Return |intersection| / |smaller set|, or 0.0 if either set is empty."""
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


# ---------------------------------------------------------------------------
# Overlap thresholds (named for tunability)
# ---------------------------------------------------------------------------

NAME_OVERLAP_GATE = 0.5         # Skip pair if name overlap below this
NAME_OVERLAP_FUZZY = 0.5        # Name overlap for arrow-flexible match
PROC_OVERLAP_FUZZY = 0.4        # Mechanism overlap for arrow-flexible match
PROC_OVERLAP_ALONE = 0.55       # Mechanism alone = duplicate regardless of name
PROC_OVERLAP_SAME_ARROW = 0.45  # Mechanism threshold when arrows match
NAME_OVERLAP_SAME_ARROW = 0.3   # Name threshold when arrows match
MECHANISM_OVERLAP_THRESHOLD = 0.55  # Pass 3 / merge mechanism-only threshold
MIN_WORDS_FOR_MECHANISM_DEDUP = 15  # Skip mechanism dedup on short text
MIN_MECHANISM_CHARS = 80        # Empty-function threshold


def is_mechanism_duplicate(words_a: set, words_b: set) -> bool:
    """Check if two mechanism word sets overlap above the threshold.

    Returns False for short texts (fewer than ``MIN_WORDS_FOR_MECHANISM_DEDUP``
    meaningful tokens) to avoid false positives on generic descriptions.
    """
    if (
        len(words_a) < MIN_WORDS_FOR_MECHANISM_DEDUP
        or len(words_b) < MIN_WORDS_FOR_MECHANISM_DEDUP
    ):
        return False
    return word_overlap(words_a, words_b) > MECHANISM_OVERLAP_THRESHOLD


# ---------------------------------------------------------------------------
# Empty-function filter
# ---------------------------------------------------------------------------


def strip_empty_functions(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Remove functions with no real mechanism content (just category names).

    A function is considered garbage if its cellular_process is shorter than
    ``MIN_MECHANISM_CHARS`` and contains no period (no real sentences), AND
    has no specific_effects or biological_consequence to compensate.
    """
    total_stripped = 0
    for interactor in payload.get("ctx_json", {}).get("interactors", []):
        funcs = interactor.get("functions", [])
        if not funcs:
            continue
        kept = []
        for f in funcs:
            cp = f.get("cellular_process") or ""
            ed = f.get("effect_description") or ""
            bc = f.get("biological_consequence") or []
            se = f.get("specific_effects") or []
            has_real_mechanism = len(cp) >= MIN_MECHANISM_CHARS or "." in cp
            has_real_effect = len(ed) >= MIN_MECHANISM_CHARS or "." in ed
            has_cascades = isinstance(bc, list) and len(bc) > 0 and any(
                isinstance(c, str) and len(c) > 30 for c in bc
            )
            has_effects = isinstance(se, list) and len(se) > 0
            if has_real_mechanism or has_real_effect or has_cascades or has_effects:
                kept.append(f)
            else:
                total_stripped += 1
        if len(kept) < len(funcs):
            interactor["functions"] = kept
    if total_stripped:
        print(
            f"   [QUALITY] Stripped {total_stripped} empty function(s) "
            f"(no real mechanism/effect/cascade content)",
            file=sys.stderr, flush=True,
        )
    return payload


# ---------------------------------------------------------------------------
# Three-pass local dedup
# ---------------------------------------------------------------------------


def deduplicate_functions_local(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Remove duplicate functions within each interactor using three-pass dedup.

    Pass 0 (quality): Strip empty functions with no real content.
    Pass 1 (fast): Exact name+arrow match.
    Pass 2 (fuzzy): Semantic name overlap + mechanism text overlap.
    Pass 3 (mechanism-only): Catches different names describing same biology.

    In-place mutation: ``interactor["functions"]`` is replaced on interactors
    where at least one duplicate was removed. Returns the payload for
    chaining.
    """
    strip_empty_functions(payload)
    total_removed = 0

    for interactor in payload.get("ctx_json", {}).get("interactors", []):
        funcs = interactor.get("functions", [])
        if len(funcs) <= 1:
            continue

        # Pass 1: exact name + arrow
        seen_sigs: set = set()
        pass1: list = []
        for f in funcs:
            sig = (
                f.get("function", "").lower().strip(),
                f.get("arrow", "").lower().strip(),
            )
            if sig not in seen_sigs:
                seen_sigs.add(sig)
                pass1.append(f)

        # Pre-compute word sets once (avoids O(N²) recomputation)
        name_words = [dedup_words(f.get("function", "")) for f in pass1]
        proc_words = [dedup_words(f.get("cellular_process", "")) for f in pass1]

        # Pass 2: fuzzy semantic dedup (keeps the claim with more evidence)
        kept_indices: list = []
        for i, f in enumerate(pass1):
            is_dup = False
            for j in kept_indices:
                if not name_words[i] or not name_words[j]:
                    continue
                n_ol = word_overlap(name_words[i], name_words[j])
                if n_ol < NAME_OVERLAP_GATE:
                    continue
                p_ol = word_overlap(proc_words[i], proc_words[j])

                if n_ol >= NAME_OVERLAP_FUZZY and p_ol >= PROC_OVERLAP_FUZZY:
                    is_dup = True
                elif p_ol >= PROC_OVERLAP_ALONE:
                    is_dup = True
                elif (
                    p_ol >= PROC_OVERLAP_SAME_ARROW
                    and n_ol >= NAME_OVERLAP_SAME_ARROW
                    and f.get("arrow") == pass1[j].get("arrow")
                ):
                    is_dup = True

                if is_dup:
                    if len(f.get("evidence", [])) > len(pass1[j].get("evidence", [])):
                        kept_indices[kept_indices.index(j)] = i
                    break
            if not is_dup:
                kept_indices.append(i)
        deduped = [pass1[i] for i in kept_indices]
        deduped_proc = [proc_words[i] for i in kept_indices]

        # Pass 3: mechanism-text-only dedup (different names, same biology)
        pass3: list = []
        pass3_proc: list = []
        for i, f in enumerate(deduped):
            pw = deduped_proc[i]
            is_dup = False
            for j, kw in enumerate(pass3_proc):
                if is_mechanism_duplicate(pw, kw):
                    is_dup = True
                    if len(f.get("evidence", [])) > len(pass3[j].get("evidence", [])):
                        pass3[j] = f
                        pass3_proc[j] = pw
                    break
            if not is_dup:
                pass3.append(f)
                pass3_proc.append(pw)
        deduped = pass3

        removed = len(funcs) - len(deduped)
        if removed:
            total_removed += removed
            interactor["functions"] = deduped

    if total_removed:
        print(
            f"   [DEDUP] Removed {total_removed} duplicate function(s) locally "
            f"(exact + fuzzy + mechanism-overlap)",
            file=sys.stderr, flush=True,
        )
    return payload
