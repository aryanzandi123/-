"""
Visualizer with original styling & behavior restored, plus:
- De-densified layout (spacing, charge, collision)
- Header/search matches index styles (title centered, round search bar)
- Nodes: dark circles + WHITE labels (as before)
- Legend restored
- Modals match original styling; two distinct modal paths:
  (1) Interaction (main ↔ interactor) when clicking the interactor link/ circle
  (2) Function (interactor → function) when clicking the function link/box
- Function confidence labels on boxes (as before)
- Arrows: pointer on hover + thicker on hover
- Function boxes connect ONLY to their interactor (never to main)
- Progress bar on viz page updated using your exact IDs
- Snapshot hydrated with ctx_json for complete function/evidence details
- Expand-on-click preserved; no depth cap — arbitrary-length chains supported.
"""
from __future__ import annotations
import json
import re
import subprocess
import sys
import time
from pathlib import Path
import tempfile

def _load_json(obj):
    if isinstance(obj, (str, bytes, Path)):
        return json.loads(Path(obj).read_text(encoding="utf-8"))
    if isinstance(obj, dict):
        return obj
    raise TypeError("json_data must be path or dict")

# JSON helper functions for data cleaning and validation
def _resolve_symbol(entry):
    """Resolves protein symbol from various field names"""
    for key in ('primary', 'hgnc_symbol', 'symbol', 'gene', 'name'):
        value = entry.get(key) if isinstance(entry, dict) else None
        if isinstance(value, str) and value.strip():
            return value.strip()
    placeholder = None
    if isinstance(entry, dict):
        placeholder = entry.get('id') or entry.get('interactor_id') or entry.get('mechanism_id')
    if placeholder:
        return f"MISSING_{placeholder}"
    return None

def _build_interactor_key(interactor):
    """Creates unique key for interactor matching"""
    if not isinstance(interactor, dict):
        return None
    pmids = interactor.get('pmids')
    if isinstance(pmids, list) and pmids:
        normalized_pmids = tuple(sorted(str(pmid) for pmid in pmids))
        return ('pmids', normalized_pmids)
    summary = interactor.get('support_summary')
    if isinstance(summary, str) and summary.strip():
        return ('summary', summary.strip())
    mechanism = interactor.get('mechanism_details')
    if isinstance(mechanism, list) and mechanism:
        return ('mechanism', tuple(sorted(mechanism)))
    return None

# Function name shortening map - REMOVED to preserve AI-generated specificity
# Previous NAME_FIXES was making specific names vague:
#   "ATXN3 Degradation" → "Degradation" (loses what's being degraded!)
#   "RNF8 Stability & DNA Repair" → "DNA repair" (loses the protein!)
#   "Apoptosis Inhibition" → "Apoptosis" (loses the arrow direction!)
# The AI prompts now generate specific, arrow-compatible names - preserve them!
NAME_FIXES = {}

def validate_function_name(name: str) -> tuple[bool, str]:
    """
    Check if function name is specific enough.
    Returns (is_valid, error_message)
    """
    if not name or not isinstance(name, str):
        return (False, "Function name is missing or invalid")

    name_lower = name.lower().strip()

    # Too short
    if len(name) < 5:
        return (False, f"Function name '{name}' is too short (< 5 chars)")

    # Check for overly generic terms without specifics
    generic_patterns = [
        ('regulation', 30),   # "Regulation" is vague unless part of longer specific name
        ('control', 25),      # "Control" is vague
        ('response', 25),     # "Response" is vague (unless specific like "DNA Damage Response")
        ('metabolism', 20),   # "Metabolism" alone is too vague
        ('signaling', 20),    # "Signaling" alone is too vague
        ('pathway', 20),      # "Pathway" alone is too vague
    ]

    for term, min_length in generic_patterns:
        if term in name_lower and len(name) < min_length:
            return (False, f"Function name '{name}' is too generic (contains '{term}' but too short)")

    # Check for very generic standalone terms
    very_generic = [
        'function', 'process', 'activity', 'mechanism', 'role',
        'involvement', 'participation', 'interaction'
    ]
    if name_lower in very_generic:
        return (False, f"Function name '{name}' is extremely generic")

    return (True, "")


def validate_interactor_quality(interactor: dict) -> list[str]:
    """
    Check for data quality issues in an interactor.
    Returns list of warning messages.
    """
    issues = []
    primary = interactor.get('primary', 'Unknown')

    # Check interactor-level confidence
    interactor_conf = interactor.get('confidence')
    if interactor_conf is not None and interactor_conf == 0:
        issues.append(f"{primary}: interaction confidence is 0 (likely data error)")

    # Check functions
    for idx, func in enumerate(interactor.get('functions', [])):
        func_name = func.get('function', f'Function #{idx}')

        # Validate function name specificity
        is_valid, msg = validate_function_name(func_name)
        if not is_valid:
            issues.append(f"{primary}/{func_name}: {msg}")

        # Validate function confidence
        fn_conf = func.get('confidence')
        if fn_conf is not None and fn_conf == 0:
            issues.append(f"{primary}/{func_name}: function confidence is 0 (likely data error)")

        # Check if arrow and function name are compatible
        arrow = func.get('arrow', '')
        if arrow in ['activates', 'inhibits']:
            # Function name should describe a process that can be activated/inhibited
            # This is a heuristic check — BUT trust the authoritative arrow
            # validator when it has already vetted this function. The
            # arrow_effect_validator checks arrows against prose verbs and
            # either confirms or corrects them; if it signed off, the name-
            # vs-arrow word overlap heuristic below would be a false positive
            # (e.g. "Prion-like Domain Interaction" can legitimately carry
            # arrow='activates' when the prose describes recruitment that
            # activates a downstream target).
            _meta = func.get('_validation_metadata') or {}
            if (
                _meta.get('validator') == 'arrow_effect_validator'
                and _meta.get('validated') is True
            ):
                continue
            # Compound names use "Mechanism & Outcome" — the arrow aligns
            # with the OUTCOME (last) clause, not the mechanism (first) one
            # (see FUNCTION_NAMING_RULES #3). Split on "&" / "/" / ";" and
            # only flag when the *last* clause still reads as a physical-
            # interaction term against an activates/inhibits arrow. This
            # stops the false positives for valid compound names like
            # "TBP-DNA Binding & Transcription Initiation" (outcome =
            # "Transcription Initiation" → activates is correct).
            incompatible_terms = ['interaction', 'binding', 'association']
            _clauses = [c.strip() for c in re.split(r'\s*[&/;]\s*', func_name) if c.strip()]
            _last_clause = (_clauses[-1] if _clauses else func_name).lower()
            if any(term in _last_clause for term in incompatible_terms):
                issues.append(f"{primary}/{func_name}: arrow='{arrow}' may not match function name (outcome clause is physical-interaction)")

    return issues


def create_visualization(json_data, output_path=None):
    """Render the visualization HTML from JSON data using Jinja2 template."""
    data = _load_json(json_data)

    if 'snapshot_json' in data:
        viz_data = data['snapshot_json']
    elif 'main' in data:
        viz_data = data
    else:
        raise ValueError("Invalid JSON structure: expected 'snapshot_json' or 'main' field")

    if not isinstance(viz_data.get('proteins'), list) or not isinstance(viz_data.get('interactions'), list):
        raise ValueError("Invalid data structure: expected 'proteins' (list) and 'interactions' (list)")

    main = viz_data.get('main', 'Unknown')
    if not main or main == 'UNKNOWN':
        main = 'Unknown'

    all_issues = []
    for interaction in viz_data.get('interactions', []):
        issues = validate_interactor_quality(interaction)
        all_issues.extend(issues)

    if all_issues:
        print(f"\n⚠️  Data Quality Warnings for {main}:")
        for issue in all_issues[:10]:
            print(f"  - {issue}")
        if len(all_issues) > 10:
            print(f"  ... and {len(all_issues) - 10} more warnings")
        print()

    raw = data

    try:
        main = (raw.get('snapshot_json') or {}).get('main') or raw.get('main') or raw.get('primary') or 'Protein'
    except Exception:
        main = raw.get('main') or raw.get('primary') or 'Protein'

    cache_bust = str(int(time.time()))

    try:
        from flask import render_template, has_app_context
        if has_app_context():
            html = render_template('visualize_legacy.html',
                main_protein=str(main),
                raw_json=raw,
                cache_bust=cache_bust,
            )
        else:
            raise RuntimeError("No app context")
    except (ImportError, RuntimeError):
        from jinja2 import Environment, FileSystemLoader
        env = Environment(loader=FileSystemLoader(str(Path(__file__).parent / 'templates')))
        tmpl = env.get_template('visualize_legacy.html')
        html = tmpl.render(main_protein=str(main), raw_json=raw, cache_bust=cache_bust)

    if output_path:
        p = Path(output_path)
        p.write_text(html, encoding='utf-8')
        return str(p.resolve())
    return html

def create_visualization_from_dict(data_dict, output_path=None):
    """
    Create visualization from dict (not file).

    NEW: Accepts dict directly from database (PostgreSQL).
    This maintains compatibility with existing frontend while enabling
    database-backed visualization.

    Args:
        data_dict: Dict with {snapshot_json: {...}, ctx_json: {...}}
        output_path: Optional output file path. If None, returns HTML content.

    Returns:
        HTML string if output_path is None, else path to saved HTML file

    Note:
        Internally calls create_visualization() which supports both
        dict input (via _load_json) and returns HTML or file path based on output_path.
    """
    if not isinstance(data_dict, dict):
        raise TypeError("data_dict must be a dict")

    # create_visualization already supports dict input via _load_json
    return create_visualization(data_dict, output_path)


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python visualizer.py <json_file> [output_html]"); raise SystemExit(2)
    src = sys.argv[1]; dst = sys.argv[2] if len(sys.argv)>2 else None
    out = create_visualization(src, dst); print("Wrote:", out)
