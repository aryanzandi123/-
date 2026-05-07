"""Prompt assembly and context building helpers for the pipeline.

Extracted from runner.py to enable reuse without circular imports.

TODO(perf): The ``build_prompt`` output contains a large stable prefix
(system guardrails + differential output rules + schema help from
``pipeline/prompts/shared_blocks.py``). ``utils/gemini_runtime.py`` already
exposes ``create_or_get_system_cache`` and ``scripts/pathway_v2/quick_assign.py``
uses it for the pathway hierarchy system message. The core discovery /
function / deep steps still send that stable prefix inline on every call.
A focused follow-up should:
  1. Build a single system-cache at pipeline start containing
     ``STRICT_GUARDRAILS``, ``DIFFERENTIAL_OUTPUT_RULES``, and any schema help.
  2. Thread the resulting cache name through ``_run_step_with_batch_transport``
     / ``_call_iterative_research_mode`` / ``_call_interaction_api`` so
     each LLM call passes ``cached_content=pipeline_cache_name`` in
     ``GenerateContentConfig``.
  3. Invalidate / recreate on cache-miss errors.
Out of scope for the current dedup / silent-failure consolidation.
"""
from __future__ import annotations

import json
from functools import lru_cache
from typing import Any, Dict, List, Optional, Tuple

from pipeline.types import StepConfig


def dumps_compact(data: Any) -> str:
    """Serialize data to compact JSON for prompts."""
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def _known_interactions_fingerprint(
    known_interactions: List[Dict[str, Any]],
) -> Tuple[str, ...]:
    """Hashable projection of known interactions for memoization.

    Only the ``primary`` symbol is rendered by
    ``build_known_interactions_context``, so the cache key only needs that.
    Everything else is ignored. Returned tuple preserves input order so
    callers that rely on insertion-order stability get the same cache hit.
    """
    return tuple(
        str(interaction.get("primary", "Unknown"))
        for interaction in known_interactions
    )


@lru_cache(maxsize=128)
def _build_known_interactions_context_cached(
    fingerprint: Tuple[str, ...],
) -> str:
    """LRU-cached inner builder — keyed on the hashable fingerprint above.

    Output is a compact flat symbol list — no numbered rows, no confidence
    scores. The model only needs to know which names to skip; extra metadata
    just inflates the prompt. Full list is emitted regardless of size
    (300 symbols × ~8 chars ≈ 2.4KB, cheap).
    """
    if not fingerprint:
        return ""

    symbols = ", ".join(fingerprint)
    total = len(fingerprint)

    return (
        "ALREADY IN DATABASE (do not re-report these — they've been discovered "
        f"in prior runs; {total} total):\n"
        f"{symbols}\n"
    )


def build_known_interactions_context(
    known_interactions: List[Dict[str, Any]],
) -> str:
    """Build exclusion context from known interactions to avoid re-searching.

    Memoized on a ``(primary, confidence)`` fingerprint of the input list —
    the same discovery run issues 3+ rounds with an identical set, so the
    previous implementation rebuilt a ~5 KB block N times. Cache size is
    bounded (``maxsize=128``) so long-running processes don't leak.

    Args:
        known_interactions: List of interaction dicts from protein database

    Returns:
        Formatted string with known interactions to skip
    """
    if not known_interactions:
        return ""
    return _build_known_interactions_context_cached(
        _known_interactions_fingerprint(known_interactions)
    )


def _slim_interactor_for_function_step(
    inter: Dict[str, Any],
    include_chain_info: bool,
) -> Dict[str, Any]:
    """Keep interactor identity + function names, strip verbose text."""
    slim: Dict[str, Any] = {
        "primary": inter.get("primary"),
        "interaction_type": inter.get("interaction_type"),
    }
    if include_chain_info:
        for key in ("upstream_interactor", "mediator_chain", "depth", "_chain_pathway"):
            if inter.get(key) is not None:
                slim[key] = inter[key]
        # Include existing chain_link_function keys so Step 2b4 knows what's done
        clf = inter.get("chain_link_functions", {})
        if clf:
            slim["_existing_chain_link_keys"] = list(clf.keys())
    functions = inter.get("functions", [])
    if functions:
        slim["functions"] = [
            {"function": f.get("function"), "arrow": f.get("arrow")}
            for f in functions
        ]
        slim["_function_count"] = len(functions)
    return slim


def _summarize_interactor(inter: Dict[str, Any]) -> Dict[str, Any]:
    """Ultra-compact summary for QC non-sampled interactors."""
    functions = inter.get("functions", [])
    return {
        "primary": inter.get("primary"),
        "interaction_type": inter.get("interaction_type"),
        "_function_count": len(functions),
        "_functions": [f.get("function") for f in functions],
    }


def _slim_interactor_for_chain_step(inter: Dict[str, Any]) -> Dict[str, Any]:
    """Atom F — compact an interactor for 2ax/2az/step2ab prompts.

    Chain steps need:
      • identity + interaction_type (so the LLM knows the graph shape)
      • chain metadata (mediator_chain, upstream_interactor, depth,
        chain_context) to reason about cascades
      • the NAMES + ARROWS + PATHWAY of existing functions so dedup
        and "already covered" judgment work — but NOT the heavy
        prose fields (cellular_process, effect_description,
        biological_consequence, specific_effects, evidence, pmids,
        arrow_context) which bloat the prompt by 10-25k tokens on a
        typical 30-interactor run.
      • chain_link_functions PRESERVED IN FULL so the LLM can see
        which hop pairs are already claimed and avoid regeneration.

    Dropping the heavy prose fields from flat ``functions[]`` frees
    ~15-25k tokens of prompt budget — enough to emit 5 full-depth
    chain-hop claims per Flash call without truncation.
    """
    slim: Dict[str, Any] = {
        "primary": inter.get("primary"),
        "interaction_type": inter.get("interaction_type"),
    }
    # Chain metadata — propagate verbatim so cascade reasoning works.
    for key in (
        "upstream_interactor",
        "mediator_chain",
        "depth",
        "chain_context",
        "_chain_pathway",
    ):
        if inter.get(key) is not None:
            slim[key] = inter[key]

    # chain_link_functions: keep intact so the LLM knows which hop
    # pair slots are already populated (and by whom).
    clf = inter.get("chain_link_functions")
    if clf:
        slim["chain_link_functions"] = clf

    # Flat functions[]: keep only identity fields — name, arrow,
    # pathway, function_context. Strip everything else.
    functions = inter.get("functions") or []
    if functions:
        slim["functions"] = [
            {
                "function": f.get("function"),
                "arrow": f.get("arrow"),
                "pathway": f.get("pathway"),
                "function_context": f.get("function_context"),
            }
            for f in functions
            if isinstance(f, dict)
        ]
        slim["_function_count"] = len(functions)
    return slim


def _compact_ctx_for_step(ctx: Dict[str, Any], step_name: str) -> Dict[str, Any]:
    """Create a step-appropriate compact view of ctx_json for the prompt.

    Strips verbose function fields (cellular_process, evidence, etc.) for
    steps that only need structural awareness. The full ctx_json is always
    preserved in current_payload for the runner's differential merge.

    Returns either the original ctx (for chain / unrecognized steps, where
    no compaction applies) or a *new* dict built by shallow-copying ctx and
    overwriting only the keys that change. The previous version deepcopied
    the entire ctx on every recognized step — a hot-path tax that grew with
    interactor count × function count × evidence depth. Because we only
    replace top-level keys (never mutate nested structures from ctx), a
    shallow copy is sufficient and preserves immutability of the caller's
    payload.
    """
    if not ctx or not isinstance(ctx, dict):
        return ctx

    # Chain resolution steps used to receive the FULL ctx (every
    # interactor's every claim's every heavy field) — that's what
    # drove 2ax/2az Flash calls over the 65k output budget before
    # they could emit full-depth claims. Atom F compacts chain-step
    # ctx too: we keep everything the LLM needs for dedup + cascade
    # context (function names, arrows, pathways, chain metadata,
    # chain_link_functions slots) and drop the heavy prose fields
    # (cellular_process, effect_description, biological_consequence,
    # specific_effects, evidence, pmids, arrow_context) from existing
    # functions[]. The LLM still sees WHICH biology is already
    # covered on each interactor; it just doesn't re-read the prose.
    is_chain_step = (
        "step2ab" in step_name
        or "chain" in step_name
        or "claim_generation" in step_name
    )
    if is_chain_step:
        interactors = ctx.get("interactors", [])
        if not isinstance(interactors, list):
            return ctx
        compact: Dict[str, Any] = dict(ctx)
        func_hist = ctx.get("function_history")
        if func_hist and isinstance(func_hist, dict):
            compact["function_history"] = {
                protein: (names[:8] if isinstance(names, list) else names)
                for protein, names in func_hist.items()
            }
        compact["interactors"] = [
            _slim_interactor_for_chain_step(inter) for inter in interactors
        ]
        return compact

    is_function_step = "function" in step_name or "step2a" in step_name
    is_deep_step = "step2b" in step_name or "deep" in step_name
    is_qc_step = "step2g" in step_name or "qc" in step_name
    is_discovery_step = "discover" in step_name or "step1" in step_name

    if not (is_function_step or is_deep_step or is_qc_step or is_discovery_step):
        return ctx

    # Shallow copy — all nested structures (function_history sub-lists,
    # individual interactor dicts, etc.) are still shared with ctx. We only
    # overwrite specific top-level keys below, and each overwrite builds a
    # brand-new value via comprehension or slicing, so the caller's ctx is
    # never mutated.
    compact: Dict[str, Any] = dict(ctx)
    interactors = ctx.get("interactors", [])
    if not isinstance(interactors, list):
        return ctx  # Malformed ctx — return uncompacted to avoid crash

    # Cap function_history to prevent token bloat (max 8 names per protein).
    # Slicing (`names[:8]`) produces a new list, so no mutation of the
    # original sub-lists.
    func_hist = ctx.get("function_history")
    if func_hist and isinstance(func_hist, dict):
        compact["function_history"] = {
            protein: (names[:8] if isinstance(names, list) else names)
            for protein, names in func_hist.items()
        }

    if is_discovery_step:
        # Discovery steps only need to know what's already been found.
        compact["interactors"] = [
            {"primary": inter.get("primary"), "interaction_type": inter.get("interaction_type")}
            for inter in interactors
        ]
    elif is_qc_step:
        import random
        if len(interactors) > 10:
            sampled = set(random.sample(range(len(interactors)), 10))
            compact["interactors"] = [
                inter if i in sampled else _summarize_interactor(inter)
                for i, inter in enumerate(interactors)
            ]
            compact["_qc_note"] = f"10 of {len(interactors)} shown in full for spot-check"
    else:
        compact["interactors"] = [
            _slim_interactor_for_function_step(inter, is_deep_step)
            for inter in interactors
        ]

    return compact


def build_prompt(
    step: StepConfig,
    prior_payload: Optional[Dict[str, Any]],
    user_query: str,
    is_first_step: bool,
    known_interactions: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """Build the prompt for this step.

    Args:
        step: Step configuration
        prior_payload: Previous step's output
        user_query: Protein name
        is_first_step: Whether this is the first step
        known_interactions: List of known interactions from database (for exclusion)
    """
    expected_fields = [field.strip() for field in step.expected_columns]
    instructions = [
        "Return ONLY valid JSON. No markdown fences, no extra text.",
        f"Keys required: {', '.join(expected_fields)}.",
    ]

    if prior_payload and "ctx_json" in prior_payload:
        ctx_for_prompt = _compact_ctx_for_step(prior_payload["ctx_json"], step.name)
        ctx_compact = dumps_compact(ctx_for_prompt)
        # Hard size guard: if context is still too large after compaction,
        # replace interactors with just the history list and a count.
        if len(ctx_compact) > 12000:
            # _compact_ctx_for_step returns a copy for recognized step types,
            # but returns the original for chain/unrecognized steps — guard here.
            if ctx_for_prompt is prior_payload["ctx_json"]:
                from copy import deepcopy
                ctx_for_prompt = deepcopy(ctx_for_prompt)
            # Strip bulky nested fields from each interactor while keeping
            # the array structure intact (so the LLM still sees names/types).
            interactors = ctx_for_prompt.get("interactors", [])
            if isinstance(interactors, list):
                _heavy_keys = ("functions", "evidence", "specific_effects",
                               "biological_consequence", "claims")
                for interactor in interactors:
                    if isinstance(interactor, dict):
                        for k in _heavy_keys:
                            interactor.pop(k, None)
            history = ctx_for_prompt.get("interactor_history", [])
            ctx_for_prompt["interactor_history"] = history[-100:]
            ctx_compact = dumps_compact(ctx_for_prompt)
        instructions.append(f"CONTEXT (from previous steps):\n{ctx_compact}")
    else:
        instructions.append("This is the first step; initialize ctx_json.")

    full_prompt = "\n".join(instructions)

    # Substitute placeholders in template
    template = step.prompt_template
    if "{user_query}" in template:
        template = template.replace("{user_query}", user_query)
    if "{ctx_json.main}" in template and prior_payload:
        main = prior_payload.get("ctx_json", {}).get("main", user_query)
        template = template.replace("{ctx_json.main}", main)
    if "{ctx_json.interactor_history}" in template and prior_payload:
        history = prior_payload.get("ctx_json", {}).get("interactor_history", [])
        template = template.replace("{ctx_json.interactor_history}", str(history))
    if "{ctx_json.function_batches}" in template and prior_payload:
        batches = prior_payload.get("ctx_json", {}).get("function_batches", [])
        template = template.replace("{ctx_json.function_batches}", str(batches))
    if "{ctx_json.function_history}" in template and prior_payload:
        func_hist = prior_payload.get("ctx_json", {}).get("function_history", {})
        template = template.replace("{ctx_json.function_history}", dumps_compact(func_hist))
    if "{ctx_json.search_history}" in template and prior_payload:
        search_hist = prior_payload.get("ctx_json", {}).get("search_history", [])
        template = template.replace("{ctx_json.search_history}", str(search_hist))

    full_prompt += "\n\n" + template

    # Add known interactions exclusion context for interactor discovery steps
    if known_interactions and ("discover" in step.name.lower() or "step1" in step.name.lower()):
        exclusion_context = build_known_interactions_context(known_interactions)
        full_prompt += exclusion_context

    return full_prompt
