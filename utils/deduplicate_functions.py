"""
AI-Powered Function Deduplication Script

Uses Gemini 3 Pro to intelligently detect and remove duplicate functions
for the same interaction, even when function names differ slightly.

If one function is more correct than another, keeps the better one.
"""

import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, List, Any, Tuple
from copy import deepcopy

try:
    from google import genai
    from utils.gemini_runtime import build_generate_content_config, get_client
    GEMINI_AVAILABLE = True
except ImportError:
    GEMINI_AVAILABLE = False
    print("Error: google-genai not installed. Run: pip install google-genai")
    sys.exit(1)

from dotenv import load_dotenv

load_dotenv()


def call_gemini_flash(prompt: str, api_key: str) -> str:
    """Call Gemini Flash for lightweight deduplication checks."""
    from pipeline.types import DEDUP_OUTPUT_SCHEMA

    client = get_client(api_key)

    response = client.models.generate_content(
        model="gemini-3-flash-preview",
        contents=prompt,
        config=build_generate_content_config(
            thinking_level="low",
            max_output_tokens=65536,
            temperature=0.0,
            response_mime_type="application/json",
            response_json_schema=DEDUP_OUTPUT_SCHEMA,
            include_thoughts=False,
        )
    )

    return response.text.strip()


def compare_functions(func1: Dict[str, Any], func2: Dict[str, Any],
                      interaction: str, api_key: str) -> Tuple[bool, int]:
    """
    Compare two functions using AI to determine if they're duplicates.

    Returns:
        (is_duplicate, better_index):
            - is_duplicate: True if functions describe the same thing
            - better_index: 1 if func1 is better, 2 if func2 is better, 0 if equal
    """
    func1_name = func1.get("function", "Unknown")
    func2_name = func2.get("function", "Unknown")

    func1_process = func1.get("cellular_process", "")
    func2_process = func2.get("cellular_process", "")

    func1_effect = func1.get("effect_description", "")
    func2_effect = func2.get("effect_description", "")

    func1_pmids = func1.get("pmids", [])
    func2_pmids = func2.get("pmids", [])

    # CRITICAL: Include direction field for proper deduplication
    func1_direction = func1.get("direction", "unknown")
    func2_direction = func2.get("direction", "unknown")

    func1_arrow = func1.get("arrow", "unknown")
    func2_arrow = func2.get("arrow", "unknown")

    func1_pathway = func1.get("pathway", "unknown")
    func2_pathway = func2.get("pathway", "unknown")

    prompt = f"""You are a molecular biology expert tasked with identifying duplicate functions.

INTERACTION: {interaction}

FUNCTION 1:
- Name: {func1_name}
- Direction: {func1_direction}
- Arrow: {func1_arrow}
- Pathway: {func1_pathway}
- Cellular Process: {func1_process}
- Effect: {func1_effect}
- PMIDs: {', '.join(map(str, func1_pmids))}

FUNCTION 2:
- Name: {func2_name}
- Direction: {func2_direction}
- Arrow: {func2_arrow}
- Pathway: {func2_pathway}
- Cellular Process: {func2_process}
- Effect: {func2_effect}
- PMIDs: {', '.join(map(str, func2_pmids))}

TASK:
1. Determine if these two functions describe the SAME biological function (even if worded differently)
2. If they are duplicates, determine which one is MORE CORRECT/COMPLETE

CRITICAL RULES:
- Functions with DIFFERENT directions (main_to_primary vs primary_to_main) are NOT duplicates
  Example: "IRE1A → Sel1L: Activates Sel1L expression" vs "Sel1L → IRE1A: Degrades IRE1A" are DIFFERENT
- Functions with SAME direction describing same process ARE duplicates
  Example: "IRE1A Protein Degradation" with direction "primary_to_main" appearing twice IS a duplicate
- Functions in DIFFERENT biological pathways are NOT duplicates even if they share a name
  Example: "Apoptosis" in "p53 Signaling" vs "Apoptosis" in "TNF Signaling" are DIFFERENT functions

IMPORTANT:
- Functions are duplicates if they describe the same biological process/outcome AND have the same direction AND the same pathway context
- Minor wording differences don't make them different functions
- "DNA Repair" and "DNA Damage Repair" are duplicates (if same pathway)
- "Autophagy" and "ER-phagy" are NOT duplicates (ER-phagy is specific)
- Different interaction directions = ALWAYS NOT duplicates
- Different pathways = ALWAYS NOT duplicates

OUTPUT FORMAT (respond with ONLY this format):
DUPLICATE: [YES or NO]
BETTER: [1 or 2 or EQUAL]
REASON: [brief explanation]

Example output:
DUPLICATE: YES
BETTER: 2
REASON: Function 2 has more specific mechanistic details and correct PMIDs.
"""

    try:
        response = call_gemini_flash(prompt, api_key)

        # Parse JSON response (API returns JSON via response_mime_type + schema)
        result = json.loads(response)
        is_duplicate = result.get("duplicate", "NO") == "YES"
        better_str = result.get("better", "EQUAL")
        better_index = {"1": 1, "2": 2}.get(better_str, 0)

        return is_duplicate, better_index

    except Exception as e:
        print(f"  ⚠ Error comparing functions: {e}", file=sys.stderr)
        # On error, assume not duplicate to be safe
        return False, 0


def batch_compare_functions(
    functions: List[Dict[str, Any]],
    interaction_name: str,
    api_key: str,
) -> List[int]:
    """Compare all functions for one interactor in a single LLM call.

    Returns list of zero-based indices to KEEP.
    """
    from pipeline.types import DEDUP_BATCH_OUTPUT_SCHEMA

    # Build function summaries for the prompt
    func_summaries = []
    for idx, f in enumerate(functions):
        func_summaries.append(
            f"[{idx}] {f.get('function', 'Unknown')} | "
            f"dir={f.get('direction', '?')} | arrow={f.get('arrow', '?')} | "
            f"pathway={f.get('pathway', '?')} | "
            f"process={str(f.get('cellular_process', ''))[:150]} | "
            f"effect={str(f.get('effect_description', ''))[:150]} | "
            f"PMIDs={len(f.get('pmids', []))}"
        )

    prompt = f"""You are a molecular biology expert identifying duplicate functions for interaction: {interaction_name}

FUNCTIONS (indexed 0 to {len(functions)-1}):
{chr(10).join(func_summaries)}

TASK: Identify groups of duplicate functions (same biological process, same direction, same pathway context) and select the BEST one to keep from each group. Functions with DIFFERENT directions or DIFFERENT pathways are NEVER duplicates.

Return:
- keep_indices: indices of all functions to keep (one per duplicate group + all unique functions)
- groups: only the duplicate groups found (omit unique functions)"""

    client = get_client(api_key)
    try:
        response = client.models.generate_content(
            model="gemini-3-flash-preview",
            contents=prompt,
            config=build_generate_content_config(
                thinking_level="low",
                max_output_tokens=65536,
                temperature=0.0,
                response_mime_type="application/json",
                response_json_schema=DEDUP_BATCH_OUTPUT_SCHEMA,
                include_thoughts=False,
            ),
        )
        result = json.loads(response.text.strip())
        keep = result.get("keep_indices", [])
        # Validate indices are in range
        valid_keep = [i for i in keep if isinstance(i, int) and 0 <= i < len(functions)]
        if valid_keep:
            return valid_keep
    except Exception as e:
        print(f"  ⚠ Batch dedup failed ({e}), keeping all functions", file=sys.stderr)

    # Fallback: keep all
    return list(range(len(functions)))


def _partition_by_function_context(
    functions: List[Dict[str, Any]],
) -> Dict[str, List[Tuple[int, Dict[str, Any]]]]:
    """Group ``functions`` by their ``function_context`` value.

    Each partition is a list of ``(original_index, function)`` tuples so
    callers can map dedup results back to positions in the original
    list. Missing / empty / unknown values fall into a single ``""``
    bucket — that's the legacy "no context label" group, kept separate
    from the canonical 'direct' / 'net' / 'chain_derived' partitions
    so legacy rows still dedup against each other.
    """
    partitions: Dict[str, List[Tuple[int, Dict[str, Any]]]] = {}
    for idx, fn in enumerate(functions):
        ctx_raw = fn.get("function_context")
        ctx = (str(ctx_raw).strip().lower() if ctx_raw else "")
        partitions.setdefault(ctx, []).append((idx, fn))
    return partitions


def deduplicate_interactor_functions(interactor: Dict[str, Any],
                                     interaction_name: str,
                                     api_key: str,
                                     verbose: bool = True) -> Dict[str, Any]:
    """
    Remove duplicate functions from a single interactor using AI comparison.

    C3: Partitions the function list by ``function_context`` BEFORE
    sending it to the LLM, so a 'direct' claim and a 'chain_derived'
    (or 'net') claim can never be merged into one row. Without this,
    dedup would silently strip the discovery-perspective label from
    one of the two — exactly what the schema's enum constraint exists
    to prevent. Each partition is deduped independently and the
    results are reassembled in the original input order.

    Returns:
        Modified interactor with duplicates removed
    """
    functions = interactor.get("functions", [])

    if len(functions) <= 1:
        return interactor

    if verbose:
        print(f"\n  Checking {len(functions)} functions for {interactor.get('primary', 'Unknown')}...")

    # Batch dedup is the only supported path. The previous pairwise fallback
    # had two problems: (1) non-transitive tie-breaking (equivalence classes
    # A≈B, B≈C were never reconciled because A vs C was never compared), and
    # (2) iteration-order dependence — shuffling the input produced different
    # dedup results for the same claims. The batch path is O(1) LLM calls per
    # interactor and deterministic given the LLM response. On batch failure,
    # ``batch_compare_functions`` already falls back to "keep all functions",
    # which is safe.
    partitions = _partition_by_function_context(functions)

    keep_original_indices: List[int] = []
    for ctx, partition in partitions.items():
        if len(partition) == 1:
            # Singleton partitions skip the LLM round-trip — there's
            # nothing to compare against.
            keep_original_indices.append(partition[0][0])
            continue

        partition_funcs = [fn for _, fn in partition]
        keep_local = batch_compare_functions(partition_funcs, interaction_name, api_key)
        for local_idx in keep_local:
            if isinstance(local_idx, int) and 0 <= local_idx < len(partition):
                keep_original_indices.append(partition[local_idx][0])

    # Sort to make the resulting list order stable with respect to the input.
    keep_functions = [functions[i] for i in sorted(set(keep_original_indices))]

    # Build the result as a shallow copy with the new functions list. The
    # caller only reads top-level fields from the returned dict, and
    # ``keep_functions`` is a freshly-built list of references to filtered
    # function dicts — neither the interactor nor any individual function is
    # mutated downstream, so a shallow copy is safe and saves the deepcopy
    # cost (which grew with interactor/function/evidence size).
    result = {**interactor, "functions": keep_functions}

    removed_count = len(functions) - len(keep_functions)
    if removed_count > 0 and verbose:
        print(f"  [OK]Removed {removed_count} duplicate function(s)")

    return result


def deduplicate_json_file(json_path: str, api_key: str,
                          output_path: str = None,
                          verbose: bool = True) -> None:
    """
    Process a JSON file and remove duplicate functions for each interaction.
    """
    json_path = Path(json_path)

    if not json_path.exists():
        print(f"Error: File not found: {json_path}", file=sys.stderr)
        sys.exit(1)

    if verbose:
        print(f"\n{'='*80}")
        print(f"AI-Powered Function Deduplication")
        print(f"{'='*80}")
        print(f"Processing: {json_path.name}")

    # Load JSON
    with open(json_path, 'r', encoding='utf-8') as f:
        data = json.load(f)

    # Get interactors from ctx_json or snapshot_json (legacy format fallback)
    ctx_json = data.get("ctx_json", {})
    snapshot_json = data.get("snapshot_json", {})

    # Try ctx_json first (new format), then snapshot_json (legacy format)
    main_protein = ctx_json.get("main") or snapshot_json.get("main", "Unknown")
    interactors = ctx_json.get("interactors") or snapshot_json.get("interactors", [])

    if not interactors:
        print("No interactors found in ctx_json or snapshot_json", file=sys.stderr)
        return

    if verbose:
        print(f"Main protein: {main_protein}")
        print(f"Found {len(interactors)} interactors\n")

    # Process each interactor
    modified_interactors = []
    total_removed = 0

    for interactor in interactors:
        primary = interactor.get("primary", "Unknown")
        interaction_name = f"{main_protein} ↔ {primary}"

        original_func_count = len(interactor.get("functions", []))

        deduplicated = deduplicate_interactor_functions(
            interactor,
            interaction_name,
            api_key,
            verbose=verbose
        )

        new_func_count = len(deduplicated.get("functions", []))
        removed = original_func_count - new_func_count
        total_removed += removed

        modified_interactors.append(deduplicated)

    # Update data - write to whichever format we read from
    if ctx_json.get("interactors") is not None:
        ctx_json["interactors"] = modified_interactors
        data["ctx_json"] = ctx_json

    # Also update snapshot_json if present (or if that's the only format)
    if "snapshot_json" in data and "interactors" in data["snapshot_json"]:
        # For legacy format, snapshot_json has the full interactor data
        if not ctx_json.get("interactors"):
            data["snapshot_json"]["interactors"] = modified_interactors
        else:
            # For new format, just update function arrays
            snapshot_lookup = {i.get("primary"): i for i in data["snapshot_json"]["interactors"]}
            for mod_int in modified_interactors:
                primary = mod_int.get("primary")
                if primary in snapshot_lookup:
                    snapshot_lookup[primary]["functions"] = mod_int.get("functions", [])

    # Save output
    if output_path is None:
        output_path = json_path

    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    if verbose:
        print(f"\n{'='*80}")
        print(f"[OK]Deduplication complete!")
        print(f"  Total duplicate functions removed: {total_removed}")
        print(f"  Saved to: {output_path}")
        print(f"{'='*80}\n")


def deduplicate_payload(
    payload: Dict[str, Any],
    api_key: str = "",
    verbose: bool = False,
    strategy: str = "llm",
) -> Dict[str, Any]:
    """Single dedup entry point for the pipeline.

    Previously, dedup lived in three places: ``runner._dedup_functions_locally``
    (word-overlap, no LLM), inline merge blocks in the iterative research
    path, and this LLM-based function. Consolidated here so every caller
    goes through one name.

    Args:
        payload: JSON payload dict with ``ctx_json``.
        api_key: Google API key — required when ``strategy="llm"``, unused
            otherwise.
        verbose: Print progress messages.
        strategy:
            - ``"llm"`` (default): LLM-based pairwise dedup via
              ``batch_compare_functions``. Slower but semantically aware —
              used in post-processing stage 4.
            - ``"local"``: fast word-overlap + mechanism-overlap dedup via
              ``utils.dedup_local.deduplicate_functions_local``. Used
              during iterative merging where LLM cost would dominate.

    Returns:
        Modified payload with duplicates removed.
    """
    if strategy == "local":
        from utils.dedup_local import deduplicate_functions_local
        return deduplicate_functions_local(payload)

    if strategy != "llm":
        raise ValueError(
            f"deduplicate_payload: unknown strategy {strategy!r}; "
            "expected 'llm' or 'local'"
        )
    # Get interactors from ctx_json
    ctx_json = payload.get("ctx_json", {})
    main_protein = ctx_json.get("main", "Unknown")
    interactors = ctx_json.get("interactors", [])

    if not interactors:
        if verbose:
            print("No interactors found - skipping deduplication", file=sys.stderr)
        return payload

    if verbose:
        print(f"\n{'='*80}")
        print(f"AI-Powered Function Deduplication")
        print(f"{'='*80}")
        print(f"Main protein: {main_protein}")
        print(f"Found {len(interactors)} interactors\n")

    # Process each interactor in parallel
    total_removed = 0
    max_dedup_workers = len(interactors)

    def _dedup_one(interactor):
        primary = interactor.get("primary", "Unknown")
        interaction_name = f"{main_protein} ↔ {primary}"
        original_func_count = len(interactor.get("functions", []))
        deduplicated = deduplicate_interactor_functions(
            interactor, interaction_name, api_key, verbose=verbose,
        )
        new_func_count = len(deduplicated.get("functions", []))
        return deduplicated, original_func_count - new_func_count

    modified_interactors = [None] * len(interactors)
    with ThreadPoolExecutor(max_workers=max_dedup_workers) as executor:
        futures = {
            executor.submit(_dedup_one, interactor): i
            for i, interactor in enumerate(interactors)
        }
        for future in as_completed(futures):
            i = futures[future]
            try:
                deduplicated, removed = future.result()
                modified_interactors[i] = deduplicated
                total_removed += removed
            except Exception as e:
                print(f"  [WARN] Dedup failed for interactor {i}: {e}", file=sys.stderr)
                modified_interactors[i] = interactors[i]
    modified_interactors = [m for m in modified_interactors if m is not None]

    # Update payload
    ctx_json["interactors"] = modified_interactors

    # Also update snapshot_json if present
    if "snapshot_json" in payload and "interactors" in payload["snapshot_json"]:
        snapshot_lookup = {i.get("primary"): i for i in payload["snapshot_json"]["interactors"]}
        for mod_int in modified_interactors:
            primary = mod_int.get("primary")
            if primary in snapshot_lookup:
                snapshot_lookup[primary]["functions"] = mod_int.get("functions", [])

    if verbose:
        print(f"\n[OK]Deduplication complete!")
        print(f"  Total duplicate functions removed: {total_removed}\n")

    return payload


def main():
    """CLI entry point"""
    if len(sys.argv) < 2:
        print("Usage: python deduplicate_functions.py <json_file> [output_file]")
        print("\nExample:")
        print("  python deduplicate_functions.py cache/ATXN3.json")
        print("  python deduplicate_functions.py cache/ATXN3.json cache/ATXN3_dedup.json")
        sys.exit(1)

    json_file = sys.argv[1]
    output_file = sys.argv[2] if len(sys.argv) > 2 else None

    if not os.getenv("GOOGLE_CLOUD_PROJECT"):
        print("Error: GOOGLE_CLOUD_PROJECT not found in environment", file=sys.stderr)
        sys.exit(1)

    api_key = os.getenv("GOOGLE_CLOUD_PROJECT")  # passed as truthy sentinel
    deduplicate_json_file(json_file, api_key, output_file, verbose=True)


if __name__ == "__main__":
    main()
