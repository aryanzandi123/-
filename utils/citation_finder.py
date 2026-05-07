#!/usr/bin/env python3
"""Gemini-powered citation finder: resolves scientific claims to verified PubMed papers.

Replaces the old title-fuzzy-matching PMID updater with a two-phase approach:
1. Gemini 3 Flash Preview + Google Search finds real papers supporting each claim.
2. NCBI E-utilities verifies every PMID and retrieves canonical titles.

Functions are NEVER deleted — if no citation is found the original evidence is
kept and a ``_citation_status`` marker is added.
"""

from __future__ import annotations

import json
import os
import sys
import time
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from utils.gemini_runtime import (
    build_generate_content_config,
    extract_text_from_generate_response,
    get_client,
    get_fallback_model,
    get_model,
    is_daily_model_quota_exhausted,
    is_quota_error,
)
from utils.pubmed_match import (
    DEFAULT_API_KEY as NCBI_DEFAULT_API_KEY,
    DEFAULT_EMAIL as NCBI_DEFAULT_EMAIL,
    DEFAULT_SLEEP as NCBI_DEFAULT_SLEEP,
    PubMedClient,
    best_match,
)
from pipeline.types import CITATION_FINDER_OUTPUT_SCHEMA


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class CitationFinderError(RuntimeError):
    """Base error for citation finder failures."""


class DailyQuotaExceededError(CitationFinderError):
    """Raised when per-model daily quota is exhausted."""


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class ClaimBundle:
    """Extracted scientific claims from a single function block."""
    function_name: str
    arrow: str
    cellular_process: str
    specific_effects: List[str]
    existing_titles: List[str]
    existing_pmids: List[str]


@dataclass
class RawCitation:
    """Citation returned by Gemini (unverified)."""
    function_name: str
    paper_title: str
    pmid: Optional[int]
    year: Optional[int]
    journal: str
    relevant_finding: str


@dataclass
class VerifiedCitation:
    """Citation after NCBI verification."""
    function_name: str
    pmid: str
    canonical_title: str
    year: Optional[int]
    journal: str
    relevant_finding: str


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

_MAX_PROCESS_CHARS = 300


def extract_claims(fn_block: Dict[str, Any]) -> ClaimBundle:
    """Pull testable scientific claims from a function dict."""
    process = fn_block.get("cellular_process") or ""
    if len(process) > _MAX_PROCESS_CHARS:
        process = process[:_MAX_PROCESS_CHARS] + "..."

    effects = fn_block.get("specific_effects") or []
    if not isinstance(effects, list):
        effects = []

    existing_evidence = fn_block.get("evidence") or []
    titles = [
        e.get("paper_title", "")
        for e in existing_evidence
        if isinstance(e, dict) and e.get("paper_title")
    ]

    existing_pmids = fn_block.get("pmids") or []
    if not isinstance(existing_pmids, list):
        existing_pmids = []

    return ClaimBundle(
        function_name=fn_block.get("function", "Unknown"),
        arrow=fn_block.get("arrow", "unknown"),
        cellular_process=process,
        specific_effects=effects[:8],
        existing_titles=titles,
        existing_pmids=[str(p) for p in existing_pmids],
    )


def build_citation_prompt(
    main_protein: str,
    interactor_name: str,
    claims: List[ClaimBundle],
) -> str:
    """Build a Gemini prompt asking for real papers supporting the claims."""
    function_blocks: List[str] = []
    for i, claim in enumerate(claims, 1):
        parts = [
            f"FUNCTION {i}: \"{claim.function_name}\" ({claim.arrow})",
            f"  Mechanism: {claim.cellular_process}" if claim.cellular_process else "",
        ]
        if claim.specific_effects:
            effects_str = "; ".join(claim.specific_effects[:5])
            parts.append(f"  Key effects: {effects_str}")
        if claim.existing_titles:
            hints = "; ".join(claim.existing_titles[:3])
            parts.append(f"  Existing paper hints (may be inaccurate): {hints}")
        function_blocks.append("\n".join(p for p in parts if p))

    functions_text = "\n\n".join(function_blocks)

    return f"""You are a biomedical citation specialist. Find REAL, PUBLISHED papers
that support these protein interaction claims. You MUST use Google Search to find
actual papers on PubMed or Google Scholar.

CRITICAL RULES:
1. Every paper you cite MUST be a real publication you found via search
2. Every PMID you provide MUST correspond to an actual PubMed record
3. If you cannot find a paper supporting a specific function, return that function
   with pmid: null and paper_title: "NOT_FOUND"
4. Prefer primary research papers over reviews when possible
5. Search for papers on PubMed using queries like: "{main_protein} {interactor_name}
   [mechanism keyword]"
6. Aim for 2-4 papers per function — quality over quantity
7. Include the PMID as an integer (e.g., 12345678), not a string

PROTEIN INTERACTION: {main_protein} <-> {interactor_name}

{functions_text}

Return a JSON object with a "citations" array. Each entry must have:
- "function_name": exact function name from above
- "paper_title": exact title from PubMed
- "pmid": integer PMID or null if not found
- "year": publication year or null
- "journal": journal name
- "relevant_finding": one sentence explaining how this paper supports the claim
"""


def parse_gemini_response(text: str) -> List[RawCitation]:
    """Parse Gemini JSON response into RawCitation objects."""
    if not text or not text.strip():
        return []

    cleaned = text.strip()
    # Strip markdown fences
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`").strip()
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:].lstrip()

    parsed: Optional[Dict[str, Any]] = None
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        # Fallback: extract first JSON object
        start = cleaned.find("{")
        end = cleaned.rfind("}") + 1
        if start >= 0 and end > start:
            try:
                parsed = json.loads(cleaned[start:end])
            except json.JSONDecodeError:
                return []

    if not parsed or not isinstance(parsed, dict):
        return []

    raw_list = parsed.get("citations", [])
    if not isinstance(raw_list, list):
        return []

    citations: List[RawCitation] = []
    for entry in raw_list:
        if not isinstance(entry, dict):
            continue
        title = entry.get("paper_title", "")
        if not title or title == "NOT_FOUND":
            continue
        pmid_val = entry.get("pmid")
        pmid_int = int(pmid_val) if pmid_val is not None else None
        citations.append(RawCitation(
            function_name=entry.get("function_name", ""),
            paper_title=title,
            pmid=pmid_int,
            year=entry.get("year"),
            journal=entry.get("journal", ""),
            relevant_finding=entry.get("relevant_finding", ""),
        ))
    return citations


def update_function_evidence(
    fn_block: Dict[str, Any],
    verified: List[VerifiedCitation],
) -> None:
    """Update a function block's evidence with verified citations.

    If verified is empty, keeps original evidence and marks ``_citation_status``.
    """
    if not verified:
        fn_block["_citation_status"] = "unresolved"
        return

    new_evidence: List[Dict[str, Any]] = []
    new_pmids: List[str] = []
    seen_pmids: set = set()

    for vc in verified:
        new_evidence.append({
            "paper_title": vc.canonical_title,
            "pmid": vc.pmid,
            "year": vc.year,
            "journal": vc.journal,
            "relevant_finding": vc.relevant_finding,
        })
        if vc.pmid and vc.pmid not in seen_pmids:
            seen_pmids.add(vc.pmid)
            new_pmids.append(vc.pmid)

    fn_block["evidence"] = new_evidence
    fn_block["pmids"] = new_pmids
    fn_block.pop("_citation_status", None)


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------

def _is_retryable(exc: BaseException) -> bool:
    """Return True for transient 429s, False for daily quota exhaustion."""
    if isinstance(exc, DailyQuotaExceededError):
        return False
    return is_quota_error(exc)


@retry(
    wait=wait_exponential(multiplier=1.5, min=2, max=30),
    stop=stop_after_attempt(3),
    retry=retry_if_exception(_is_retryable),
    reraise=True,
)
def call_gemini_citation_search(
    prompt: str,
    api_key: str,
    *,
    verbose: bool = False,
    request_metrics: Optional[Dict[str, int]] = None,
) -> List[RawCitation]:
    """Call Gemini 3 Flash Preview with Google Search to find real papers."""
    model_id = get_model("flash")
    client = get_client(api_key)

    config = build_generate_content_config(
        thinking_level="medium",
        temperature=0.5,
        use_google_search=True,
        max_output_tokens=65536,
        response_mime_type="application/json",
        response_json_schema=CITATION_FINDER_OUTPUT_SCHEMA,
    )

    if verbose:
        print(f"  [CITATION] Calling {model_id} with Google Search...", flush=True)

    try:
        if request_metrics is not None:
            request_metrics["citation_gemini_calls"] = (
                request_metrics.get("citation_gemini_calls", 0) + 1
            )
        response = client.models.generate_content(
            model=model_id,
            contents=prompt,
            config=config,
        )
        text = extract_text_from_generate_response(response)
        return parse_gemini_response(text)
    except Exception as e:
        if is_daily_model_quota_exhausted(e):
            fallback = get_fallback_model("flash")
            if fallback and fallback != model_id:
                print(f"  [FALLBACK] {model_id} quota hit, trying {fallback}")
                try:
                    response = client.models.generate_content(
                        model=fallback, contents=prompt, config=config,
                    )
                    text = extract_text_from_generate_response(response)
                    return parse_gemini_response(text)
                except Exception:
                    pass
            raise DailyQuotaExceededError(
                f"Daily quota exhausted for {model_id}: {e}"
            ) from e
        if is_quota_error(e):
            raise CitationFinderError(f"Transient quota error on {model_id}: {e}") from e
        raise CitationFinderError(f"Citation search failed on {model_id}: {e}") from e


def verify_pmids_batch(
    raw_citations: List[RawCitation],
    ncbi_client: PubMedClient,
    *,
    request_metrics: Optional[Dict[str, int]] = None,
) -> List[VerifiedCitation]:
    """Verify PMIDs via NCBI and return only confirmed citations."""
    if not raw_citations:
        return []

    # Phase 1: batch-fetch all PMIDs that Gemini provided
    pmids_to_check = [
        str(rc.pmid) for rc in raw_citations if rc.pmid is not None
    ]
    ncbi_titles: Dict[str, str] = {}
    if pmids_to_check:
        try:
            if request_metrics is not None:
                request_metrics["citation_ncbi_calls"] = (
                    request_metrics.get("citation_ncbi_calls", 0) + 1
                )
            ncbi_titles = ncbi_client.fetch_titles(pmids_to_check)
        except Exception as exc:
            print(f"  [WARN] NCBI batch fetch failed: {exc}", file=sys.stderr)

    verified: List[VerifiedCitation] = []
    failed_citations: List[Dict[str, str]] = []

    for rc in raw_citations:
        pmid_str = str(rc.pmid) if rc.pmid is not None else None

        # Case 1: PMID exists in NCBI
        if pmid_str and pmid_str in ncbi_titles:
            verified.append(VerifiedCitation(
                function_name=rc.function_name,
                pmid=pmid_str,
                canonical_title=ncbi_titles[pmid_str],
                year=rc.year,
                journal=rc.journal,
                relevant_finding=rc.relevant_finding,
            ))
            continue

        # Case 2: PMID not found or missing — try title search fallback
        if rc.paper_title and rc.paper_title != "NOT_FOUND":
            try:
                if request_metrics is not None:
                    request_metrics["citation_ncbi_calls"] = (
                        request_metrics.get("citation_ncbi_calls", 0) + 1
                    )
                ids = ncbi_client.search_ids(rc.paper_title, 5)
                if ids:
                    titles = ncbi_client.fetch_titles(ids)
                    match = best_match(rc.paper_title, titles)
                    if match.pmid and match.similarity > 0.5:
                        verified.append(VerifiedCitation(
                            function_name=rc.function_name,
                            pmid=str(match.pmid) if match.pmid else "",
                            canonical_title=match.matched_title or rc.paper_title,
                            year=rc.year,
                            journal=rc.journal,
                            relevant_finding=rc.relevant_finding,
                        ))
                        continue
            except Exception as exc:
                print(f"  [WARN] NCBI title search failed for '{rc.paper_title[:60]}': {exc}", file=sys.stderr)
                failed_citations.append({
                    "function": rc.function_name,
                    "title": rc.paper_title[:100],
                    "error": str(exc),
                })
                continue

        # Case 3: neither verification worked — track as unverified
        failed_citations.append({
            "function": rc.function_name,
            "title": (rc.paper_title or "")[:100],
            "error": "no_match",
        })

    if failed_citations and request_metrics is not None:
        request_metrics["citation_failures"] = len(failed_citations)

    return verified


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def find_citations_for_interactor(
    interactor: Dict[str, Any],
    main_protein: str,
    api_key: str,
    ncbi_client: PubMedClient,
    *,
    verbose: bool = False,
    request_metrics: Optional[Dict[str, int]] = None,
) -> None:
    """Find and verify citations for all functions of one interactor (mutates in-place)."""
    interactor_name = interactor.get("primary", "Unknown")
    functions = interactor.get("functions")
    if not isinstance(functions, list) or not functions:
        return

    # Extract claims from all functions
    claims = [extract_claims(fn) for fn in functions]
    if not claims:
        return

    # Call Gemini once for this interactor
    prompt = build_citation_prompt(main_protein, interactor_name, claims)

    try:
        raw_citations = call_gemini_citation_search(
            prompt, api_key, verbose=verbose, request_metrics=request_metrics,
        )
    except DailyQuotaExceededError:
        raise  # propagate up so orchestrator can stop
    except CitationFinderError as e:
        print(f"  [WARN] Citation search failed for {interactor_name}: {e}", file=sys.stderr)
        for fn in functions:
            fn["_citation_status"] = "gemini_error"
        return

    if not raw_citations:
        if verbose:
            print(f"  [WARN] Gemini returned no citations for {interactor_name}", flush=True)
        for fn in functions:
            fn["_citation_status"] = "unresolved"
        return

    # Verify via NCBI
    verified = verify_pmids_batch(
        raw_citations, ncbi_client, request_metrics=request_metrics,
    )

    if verbose:
        print(
            f"  [CITATION] {interactor_name}: {len(raw_citations)} raw -> "
            f"{len(verified)} verified",
            flush=True,
        )

    # Group verified citations by function name (case-insensitive)
    by_function: Dict[str, List[VerifiedCitation]] = {}
    for vc in verified:
        key = vc.function_name.strip().lower()
        by_function.setdefault(key, []).append(vc)

    # Update each function's evidence
    for fn in functions:
        fn_key = fn.get("function", "").strip().lower()
        matched = by_function.get(fn_key, [])
        update_function_evidence(fn, matched)

    # Merge function-derived PMIDs into interactor-level list (preserve existing)
    existing_pmids = interactor.get("pmids", [])
    seen: set = set(existing_pmids)
    merged_pmids = list(existing_pmids)
    for fn in functions:
        for p in fn.get("pmids", []):
            if p and p not in seen:
                seen.add(p)
                merged_pmids.append(p)
    if merged_pmids:
        interactor["pmids"] = merged_pmids


def find_and_verify_citations(
    payload: Dict[str, Any],
    api_key: str,
    *,
    verbose: bool = False,
    batch_size: int = int(os.getenv("CITATION_BATCH_SIZE", "4")),
    step_logger: Any = None,
    **_kw: Any,
) -> Dict[str, Any]:
    """Top-level entry point: process entire payload, return updated payload."""
    if not api_key:
        print("[WARN] No API key for citation finder, skipping.", file=sys.stderr)
        return payload

    ctx = payload.get("ctx_json")
    if not isinstance(ctx, dict):
        print("[WARN] No ctx_json found, skipping citation finder.", file=sys.stderr)
        return payload

    main_protein = ctx.get("main", "Unknown")
    interactors = ctx.get("interactors", [])
    if not interactors:
        return payload

    batch_delay = float(os.getenv("CITATION_BATCH_DELAY", "1.5"))
    model_id = get_model("flash")

    ncbi_client = PubMedClient(
        email=NCBI_DEFAULT_EMAIL,
        api_key=NCBI_DEFAULT_API_KEY,
        sleep=NCBI_DEFAULT_SLEEP,
    )

    request_metrics: Dict[str, int] = {
        "citation_gemini_calls": 0,
        "citation_ncbi_calls": 0,
        "citation_verified_count": 0,
        "citation_unresolved_count": 0,
    }

    print(f"\n{'='*60}")
    print(f"CITATION FINDER: {main_protein}")
    print(f"  Model: {model_id} + Google Search")
    print(f"  Interactors: {len(interactors)}, batch_size={batch_size}")
    print(f"{'='*60}")

    quota_exhausted = False

    for idx, interactor in enumerate(interactors):
        if quota_exhausted:
            for fn in interactor.get("functions", []):
                fn["_citation_status"] = "skipped_quota"
            continue

        try:
            find_citations_for_interactor(
                interactor, main_protein, api_key, ncbi_client,
                verbose=verbose, request_metrics=request_metrics,
            )
        except DailyQuotaExceededError as e:
            quota_exhausted = True
            print(f"[WARN] Quota exhausted at interactor {idx + 1}: {e}", file=sys.stderr)
            for fn in interactor.get("functions", []):
                fn.setdefault("_citation_status", "skipped_quota")
            continue
        except Exception as e:
            print(f"[WARN] Unexpected error for {interactor.get('primary', '?')}: {e}", file=sys.stderr)

        # Delay between interactors (not after the last one)
        if idx < len(interactors) - 1 and not quota_exhausted:
            time.sleep(batch_delay)

    # Count final stats
    for interactor in interactors:
        for fn in interactor.get("functions", []):
            if fn.get("_citation_status"):
                request_metrics["citation_unresolved_count"] += 1
            else:
                request_metrics["citation_verified_count"] += len(fn.get("pmids", []))

    # Update snapshot_json to match
    if "snapshot_json" in payload:
        payload["snapshot_json"]["interactors"] = deepcopy(interactors)

    # Merge metrics
    existing = payload.get("_request_metrics", {})
    if not isinstance(existing, dict):
        existing = {}
    for k, v in request_metrics.items():
        existing[k] = existing.get(k, 0) + v
    payload["_request_metrics"] = existing

    print(f"\n{'='*60}")
    print("CITATION FINDER SUMMARY")
    print(f"  Gemini calls: {request_metrics['citation_gemini_calls']}")
    print(f"  NCBI calls:   {request_metrics['citation_ncbi_calls']}")
    print(f"  Verified:     {request_metrics['citation_verified_count']}")
    print(f"  Unresolved:   {request_metrics['citation_unresolved_count']}")
    print(f"{'='*60}\n")

    return payload
