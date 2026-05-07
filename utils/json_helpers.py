"""Pure JSON parsing and merging helpers for the pipeline.

Extracted from runner.py to enable reuse without circular imports.
"""
from __future__ import annotations

import json
from copy import deepcopy
from typing import Any, Dict, List, Optional, Tuple


class PipelineError(RuntimeError):
    """Raised when a pipeline step fails validation or parsing."""


def _evidence_dedup_key(ev: Dict[str, Any]) -> str:
    """Return a dedup key for an evidence item: PMID if available, else content hash."""
    if not isinstance(ev, dict):
        return str(ev)
    pmid = ev.get("pmid") or ev.get("id")
    if pmid:
        return f"pmid:{pmid}"
    title = (ev.get("paper_title") or "").strip().lower()
    year = str(ev.get("year") or "")
    return f"content:{title}|{year}"


# ---------------------------------------------------------------------------
# H4: chain_link_functions cross-batch merge helpers
# ---------------------------------------------------------------------------
# When two pipeline batches produce overlapping chain_link_functions for
# the same canonical pair, the previous merge silently dropped any
# entry whose function name had already been seen — losing the second
# batch's richer cellular_process text, additional evidence, and pmids.
# These helpers make the merge idempotent: same-key entries fold into
# one entry that keeps the longer text and unions the lists.


def _chain_link_fn_key(fn: Dict[str, Any]) -> Tuple[str, str]:
    """Dedup key for a chain_link_functions entry: (name, function_context).

    Aligns with the schema's ``uq_claim_interaction_fn_pw_ctx`` index
    semantics so a 'direct' and a 'chain_derived' entry sharing the
    same function name never collapse into one row. Both halves are
    case-folded; missing values become empty strings (not ``None``)
    so dict-key comparisons are consistent.
    """
    name = (fn.get("function") or "").strip().lower()
    ctx = (fn.get("function_context") or "").strip().lower()
    return (name, ctx)


def _merge_chain_link_function(
    existing: Dict[str, Any],
    incoming: Dict[str, Any],
) -> Dict[str, Any]:
    """Merge two chain_link_functions entries that share the same key.

    The merge is idempotent and order-independent:
      • Long-text fields (cellular_process, effect_description,
        mechanism) keep whichever side has the longer non-empty value.
      • Single-value fields (arrow, pathway, function_context, …)
        prefer the existing value but fall back to incoming when
        existing is empty.
      • List fields (biological_consequence, specific_effects) union
        with case-insensitive content dedup.
      • evidence unions via ``_evidence_dedup_key``.
      • pmids unions while preserving the existing order.

    Returns a new dict; does not mutate either input.
    """
    result = dict(existing)

    for field in ("cellular_process", "effect_description", "mechanism"):
        ev = (result.get(field) or "").strip()
        iv = (incoming.get(field) or "").strip()
        if len(iv) > len(ev):
            result[field] = incoming.get(field)

    for field in (
        "arrow",
        "pathway",
        "function_context",
        "interaction_effect",
        "direction",
        "likely_direction",
        "interaction_direction",
    ):
        if not result.get(field) and incoming.get(field):
            result[field] = incoming[field]

    for field in ("biological_consequence", "specific_effects"):
        existing_list = list(result.get(field, []) or [])
        incoming_list = list(incoming.get(field, []) or [])
        seen_items = {str(x).strip().lower() for x in existing_list}
        for item in incoming_list:
            key = str(item).strip().lower()
            if key and key not in seen_items:
                existing_list.append(item)
                seen_items.add(key)
        result[field] = existing_list

    existing_ev = list(result.get("evidence", []) or [])
    seen_ev_keys = {
        _evidence_dedup_key(e) for e in existing_ev if isinstance(e, dict)
    }
    for ev in (incoming.get("evidence", []) or []):
        if not isinstance(ev, dict):
            continue
        key = _evidence_dedup_key(ev)
        if key not in seen_ev_keys:
            existing_ev.append(ev)
            seen_ev_keys.add(key)
    result["evidence"] = existing_ev

    existing_pmids = list(result.get("pmids", []) or [])
    seen_pmids = set(existing_pmids)
    for pmid in (incoming.get("pmids", []) or []):
        if pmid and pmid not in seen_pmids:
            existing_pmids.append(pmid)
            seen_pmids.add(pmid)
    result["pmids"] = existing_pmids

    return result


def strip_code_fences(text: str) -> str:
    """Remove surrounding Markdown code fences if present."""
    if text is None:
        return ""
    stripped = text.strip()
    if stripped.startswith("```") and stripped.endswith("```"):
        stripped = stripped.strip("`").strip()
        if stripped.lower().startswith("json"):
            stripped = stripped[4:].lstrip()
        elif stripped.lower().startswith("csv"):
            stripped = stripped[3:].lstrip()
    return stripped


def repair_truncated_json(text: str) -> str:
    """Attempt to close an incomplete JSON string by balancing delimiters.

    Uses a character-level state machine to track open braces/brackets and
    unclosed strings, then appends the necessary closing characters.
    Returns the original text unchanged if it already parses or contains
    no opening delimiter.
    """
    if not text or "{" not in text and "[" not in text:
        return text

    # Quick check: if it already parses, return as-is
    try:
        json.JSONDecoder().raw_decode(text.lstrip())
        return text
    except json.JSONDecodeError:
        pass

    in_string = False
    escape = False
    stack: List[str] = []

    for ch in text:
        if escape:
            escape = False
            continue
        if in_string:
            if ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch in ("{", "["):
            stack.append(ch)
        elif ch == "}" and stack and stack[-1] == "{":
            stack.pop()
        elif ch == "]" and stack and stack[-1] == "[":
            stack.pop()

    if not stack and not in_string:
        return text  # Already balanced — nothing to repair

    repaired = text

    # Close any open string
    if in_string:
        repaired += '"'

    # Remove trailing incomplete fragments: dangling comma, colon, partial key
    import re
    repaired = re.sub(r',\s*"[^"]*"?\s*:?\s*$', "", repaired)
    repaired = re.sub(r',\s*$', "", repaired)

    # Close open delimiters in reverse order
    for opener in reversed(stack):
        repaired += "]" if opener == "[" else "}"

    return repaired


def deep_merge_interactors(
    existing_interactors: List[Dict[str, Any]],
    new_interactors: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Intelligently merge new interactors into existing list.

    - If interactor (by 'primary' key) doesn't exist, add it
    - If interactor exists, merge new fields and append new functions
    """
    # Create lookup by primary key
    interactor_map = {i.get("primary"): deepcopy(i) for i in existing_interactors}

    for new_int in new_interactors:
        primary_key = new_int.get("primary")
        if not primary_key:
            continue

        if primary_key in interactor_map:
            # Merge into existing interactor
            existing = interactor_map[primary_key]

            # Merge functions (update in place by signature; append new ones)
            existing_functions = existing.get("functions", []) or []
            new_functions = new_int.get("functions", []) or []

            def _norm_str(v: Any) -> str:
                return str(v or "").strip().lower()

            def _norm_dir(fn: Dict[str, Any]) -> str:
                # S1: bidirectional is dead — fold into main_to_primary
                d = _norm_str(fn.get("interaction_direction") or fn.get("direction"))
                if "primary_to_main" in d or d == "p2m" or d == "b_to_a":
                    return "primary_to_main"
                if "main_to_primary" in d or d == "m2p" or d == "a_to_b":
                    return "main_to_primary"
                return "main_to_primary"

            def _fn_signature(fn: Dict[str, Any]) -> str:
                if "mechanism_id" in fn and fn.get("mechanism_id"):
                    return f"id:{_norm_str(fn.get('mechanism_id'))}|dir:{_norm_dir(fn)}"
                name = _norm_str(fn.get("function"))
                proc = _norm_str(fn.get("cellular_process"))
                return f"name:{name}|proc:{proc}|dir:{_norm_dir(fn)}"

            # Build index for existing functions
            existing_index: Dict[str, Dict[str, Any]] = {}
            for ef in existing_functions:
                if isinstance(ef, dict):
                    existing_index[_fn_signature(ef)] = ef

            # Determine context type for tagging
            interaction_type = new_int.get("interaction_type", "direct")
            upstream = new_int.get("upstream_interactor")
            mediator_chain = new_int.get("mediator_chain", [])
            context_type = "chain" if (interaction_type == "indirect" or upstream or mediator_chain) else "direct"

            for nf in new_functions:
                if not isinstance(nf, dict):
                    continue
                sig = _fn_signature(nf)
                if sig in existing_index:
                    base = existing_index[sig]
                    # Update core directional/effect fields if provided
                    for k in ("arrow", "interaction_effect", "direction", "interaction_direction", "intent"):
                        v = nf.get(k)
                        if v not in (None, ""):
                            base[k] = v
                    # Merge pmids
                    base_pmids = set(base.get("pmids", []) or [])
                    new_pmids = set(nf.get("pmids", []) or [])
                    if new_pmids:
                        base["pmids"] = sorted(list(base_pmids.union(new_pmids)))
                    # Merge specific_effects
                    base_se = set(base.get("specific_effects", []) or [])
                    new_se = set(nf.get("specific_effects", []) or [])
                    if new_se:
                        base["specific_effects"] = sorted(list(base_se.union(new_se)))
                    # Merge biological_consequence
                    base_bc = set(map(str, (base.get("biological_consequence", []) or [])))
                    new_bc = set(map(str, (nf.get("biological_consequence", []) or [])))
                    if new_bc:
                        base["biological_consequence"] = sorted(list(base_bc.union(new_bc)))
                    # Merge evidence by PMID/id key
                    def _ek(e: Dict[str, Any]) -> str:
                        return str((e or {}).get("pmid") or (e or {}).get("id") or "")
                    base_ev = base.get("evidence", []) or []
                    ev_map = { _ek(e): e for e in base_ev if isinstance(e, dict) }
                    for e in (nf.get("evidence", []) or []):
                        if isinstance(e, dict):
                            k = _ek(e)
                            if k and k in ev_map:
                                if len(str(e)) > len(str(ev_map[k])):
                                    ev_map[k] = e
                            else:
                                ev_map[k] = e
                    base["evidence"] = list(ev_map.values())
                else:
                    # Tag and append as new function
                    if "_context" not in nf:
                        nf["_context"] = {
                            "type": context_type,
                            "query_protein": new_int.get("_query_protein"),
                            "chain": mediator_chain if mediator_chain else None,
                        }
                    existing_functions.append(nf)

            existing["functions"] = existing_functions

            # Update other fields (take newer values)
            for key, value in new_int.items():
                if key == "functions":
                    continue  # Already handled
                elif key == "pmids":
                    # Merge PMIDs (union)
                    existing_pmids = existing.get("pmids", [])
                    existing["pmids"] = list(set(existing_pmids + value))
                elif key == "evidence":
                    # Merge evidence with dedup (PMID + content-hash fallback)
                    existing_evidence = existing.get("evidence", [])
                    existing_keys = {_evidence_dedup_key(e) for e in existing_evidence}
                    existing["evidence"] = existing_evidence + [
                        e for e in value if _evidence_dedup_key(e) not in existing_keys
                    ]
                elif key == "interaction_type":
                    # Allow upgrading direct→indirect when function evidence warrants it
                    if not existing.get("interaction_type") and value in ["direct", "indirect"]:
                        existing[key] = value
                    elif (
                        existing.get("interaction_type") == "direct"
                        and value == "indirect"
                        and new_int.get("upstream_interactor")
                    ):
                        # Reclassify: function mapping found this is actually indirect
                        existing[key] = value
                elif key == "upstream_interactor":
                    # Update upstream_interactor when reclassifying or filling in
                    if value and (
                        not existing.get("upstream_interactor")
                        or existing.get("interaction_type") == "indirect"
                    ):
                        existing[key] = value
                elif key == "chain_link_functions":
                    # Merge chain_link_functions dicts, canonicalizing every
                    # key to its direction-agnostic form at ingest. Two
                    # batches that produce data for the same pair under
                    # opposite directional keys ("A->B" vs "B->A") now land
                    # under the same canonical key ("A|B") and get merged
                    # into one function list instead of two parallel ones.
                    #
                    # H4: when the SAME pair has the SAME function_name +
                    # function_context across two batches, merge their
                    # contents into one entry (longer text wins, evidence
                    # / pmids unioned) instead of silently dropping the
                    # second one. This makes the merge idempotent — the
                    # crash-save retry path can re-emit a batch without
                    # losing the richer text the first run produced.
                    from utils.chain_resolution import (
                        canonicalize_chain_link_functions,
                    )
                    existing_canon = canonicalize_chain_link_functions(
                        existing.get("chain_link_functions") or {}
                    )
                    new_canon = canonicalize_chain_link_functions(value or {})
                    merged = {**existing_canon}
                    for pair_key, funcs in new_canon.items():
                        if pair_key not in merged:
                            merged[pair_key] = list(funcs)
                            continue
                        # Index existing entries by (name, function_context)
                        # so the dedup match aligns with the schema's
                        # uq_claim_interaction_fn_pw_ctx semantics.
                        existing_index: Dict[Tuple[str, str], int] = {}
                        for i, f in enumerate(merged[pair_key]):
                            if not isinstance(f, dict):
                                continue
                            fk = _chain_link_fn_key(f)
                            if fk[0]:
                                existing_index[fk] = i
                        for f in funcs:
                            if not isinstance(f, dict):
                                continue
                            fk = _chain_link_fn_key(f)
                            if not fk[0]:
                                # Anonymous entry — append as-is.
                                merged[pair_key].append(f)
                                continue
                            if fk in existing_index:
                                idx = existing_index[fk]
                                merged[pair_key][idx] = _merge_chain_link_function(
                                    merged[pair_key][idx], f,
                                )
                            else:
                                merged[pair_key].append(f)
                                existing_index[fk] = len(merged[pair_key]) - 1
                    existing[key] = merged
                else:
                    # Chain-state fields are derived from the canonical chain
                    # annotations — never clobber an existing populated value
                    # with an empty/null refresh from an LLM response that
                    # omitted them. Fixes the A5 hazard where step2a recovery
                    # passes, Tier-2 nested pipelines, and citation-verification
                    # responses silently wiped chain_context off the interactor
                    # so the downstream 2ax/2az enumerator saw an empty view
                    # and skipped every hop past the first.
                    if key in ("chain_context", "mediator_chain",
                              "upstream_interactor", "depth"):
                        if value in (None, "", [], {}):
                            continue
                    # Overwrite with new value
                    existing[key] = value
        else:
            # New interactor - add it
            interactor_map[primary_key] = new_int

    return list(interactor_map.values())


def parse_json_output(
    text: str,
    expected_fields: List[str],
    previous_payload: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Parse model output into a JSON object, merging with prior payload when needed.

    This function performs DIFFERENTIAL MERGING:
    - New interactors are added to the list
    - Existing interactors get new functions appended
    - Tracking lists (interactor_history, etc.) are appended
    """
    cleaned = strip_code_fences(text)

    # Handle None or empty input (e.g., from cancelled jobs)
    if not cleaned or not cleaned.strip():
        raise PipelineError("Empty or null model output (job may have been cancelled).")

    required = [field for field in expected_fields if field != "ndjson"]

    def _scan_json_dicts(source: str) -> List[Dict[str, Any]]:
        """Scan *source* for JSON dicts, returning all found segments.

        P1-B3: scans the entire source rather than breaking after the
        first segment containing the required fields. Multi-segment LLM
        responses ("here's chunk 1 ... and here's chunk 2") would
        previously lose every chunk after the first complete one. The
        scanner already handles garbage between dicts via the
        ``except JSONDecodeError`` advance, so scanning to end is safe.
        """
        decoder = json.JSONDecoder()
        idx = 0
        segments: List[Dict[str, Any]] = []
        while idx < len(source):
            try:
                obj, end_idx = decoder.raw_decode(source, idx)
                idx = end_idx
                while idx < len(source) and source[idx] in (" ", "\n", "\r", "\t"):
                    idx += 1
                if isinstance(obj, dict):
                    segments.append(obj)
                elif isinstance(obj, list):
                    segments.extend(item for item in obj if isinstance(item, dict))
            except json.JSONDecodeError:
                idx += 1
        return segments

    data_segments = _scan_json_dicts(cleaned)
    has_required = any(all(f in seg for f in required) for seg in data_segments)

    # Fallback: attempt to repair truncated JSON when segments are empty
    # or none of them contain the required fields.
    if not has_required:
        import sys
        recovered = False
        # Try repairing from each '{' position (handles garbage prefixes)
        for pos in range(len(cleaned)):
            if cleaned[pos] != "{":
                continue
            candidate = cleaned[pos:]
            repaired = repair_truncated_json(candidate)
            if repaired != candidate:
                repaired_segments = _scan_json_dicts(repaired)
                if repaired_segments and any(any(f in seg for f in required) for seg in repaired_segments):
                    data_segments = repaired_segments
                    recovered = True
                    print(
                        "[WARN] Recovered from truncated JSON output",
                        file=sys.stderr,
                        flush=True,
                    )
                    break

    if not data_segments:
        open_b = cleaned.count("{") - cleaned.count("}")
        raise PipelineError(
            f"No valid JSON found in model output. "
            f"Length: {len(cleaned)} chars. "
            f"Unbalanced braces: {open_b}. "
            f"Start: {cleaned[:200]!r}"
        )

    # Merge all segments from this step's output
    step_output: Dict[str, Any] = {}
    for segment in data_segments:
        if not isinstance(segment, dict):
            continue
        for key, value in segment.items():
            if key in step_output and isinstance(step_output[key], dict) and isinstance(value, dict):
                step_output[key].update(value)
            else:
                step_output[key] = value

    # DIFFERENTIAL MERGE with previous payload
    if previous_payload:
        merged: Dict[str, Any] = deepcopy(previous_payload)

        # Handle ctx_json specially (intelligent merge)
        if "ctx_json" in step_output:
            new_ctx = step_output["ctx_json"]
            existing_ctx = merged.get("ctx_json", {})

            # Always use new 'main' if provided
            if "main" in new_ctx:
                existing_ctx["main"] = new_ctx["main"]

            # Merge interactors intelligently
            if "interactors" in new_ctx:
                existing_interactors = existing_ctx.get("interactors", [])
                new_interactors = new_ctx["interactors"]
                existing_ctx["interactors"] = deep_merge_interactors(existing_interactors, new_interactors)

            # Append to tracking lists
            for list_key in ["interactor_history", "function_batches", "search_history"]:
                if list_key in new_ctx:
                    existing_list = existing_ctx.get(list_key, [])
                    new_items = new_ctx[list_key]
                    # Append unique items
                    existing_set = set(existing_list) if existing_list else set()
                    existing_ctx[list_key] = existing_list + [x for x in new_items if x not in existing_set]

            # Merge function_history (dict of lists)
            if "function_history" in new_ctx:
                existing_func_hist = existing_ctx.get("function_history", {})
                new_func_hist = new_ctx["function_history"]
                for protein, funcs in new_func_hist.items():
                    if protein in existing_func_hist:
                        existing_func_hist[protein].extend(funcs)
                    else:
                        existing_func_hist[protein] = funcs
                existing_ctx["function_history"] = existing_func_hist

            merged["ctx_json"] = existing_ctx

        # Merge other top-level fields
        for key, value in step_output.items():
            if key == "ctx_json":
                continue  # Already handled
            merged[key] = value

        result = merged
    else:
        result = step_output

    # Validate required fields
    missing = [field for field in required if field not in result]
    if missing:
        raise PipelineError(f"Missing required fields in output: {missing}")

    return result
