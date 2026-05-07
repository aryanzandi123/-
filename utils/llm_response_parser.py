"""
Utilities for parsing JSON responses from LLM models.

L3.1 — six-strategy salvage chain:
    1. Whole text as JSON.
    2. Strip code fences, retry whole text.
    3. Outermost ``{...}`` substring.
    4. Outermost ``[...]`` substring (handles bare-array responses).
    5. Regex scan for known top-level keys' arrays
       (``"interactors": [...]``, ``"functions": [...]``, ...).
    6. Last-resort: brace-balanced incremental scan returning the longest
       valid JSON-object prefix found anywhere in the text.

Each strategy logs its name on success via stderr (prefixed
``[JSON SALVAGE]``) so operators can see which path rescued a malformed
response and tune accordingly.
"""
import json
import re
import sys
from typing import Any, Dict, Optional


_KNOWN_ARRAY_KEYS = (
    "interactors",
    "functions",
    "chain_link_functions",
    "indirect_interactors",
    "_chain_annotations_explicit",
    "_chain_annotations_hidden",
)


def _strategy_whole(text: str) -> Optional[Any]:
    try:
        return json.loads(text)
    except Exception:
        return None


def _strategy_strip_fences(text: str) -> Optional[Any]:
    cleaned = text.strip()
    if cleaned.startswith("```") and cleaned.endswith("```"):
        cleaned = cleaned.strip("`").strip()
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:].lstrip()
    try:
        return json.loads(cleaned)
    except Exception:
        return None


def _strategy_outer_object(text: str) -> Optional[Any]:
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except Exception:
            return None
    return None


def _strategy_outer_array(text: str) -> Optional[Any]:
    start = text.find("[")
    end = text.rfind("]")
    if start >= 0 and end > start:
        try:
            arr = json.loads(text[start:end + 1])
            # Wrap bare arrays into the canonical ctx_json shape so callers
            # can treat the result uniformly.
            return {"ctx_json": {"interactors": arr}, "_salvage_wrapped_array": True}
        except Exception:
            return None
    return None


def _strategy_keyed_arrays(text: str) -> Optional[Any]:
    """Scan for the bare ``"key": [...]`` patterns of known top-level arrays
    and rebuild a minimal object containing whatever we can extract."""
    out: Dict[str, Any] = {}
    for key in _KNOWN_ARRAY_KEYS:
        # Greedy bracket match from the first ``"key": [`` to its matching
        # closing bracket. Use a simple bracket-depth scan because re alone
        # cannot match balanced brackets.
        m = re.search(rf'"{re.escape(key)}"\s*:\s*\[', text)
        if not m:
            continue
        start = m.end() - 1  # index of '['
        depth = 0
        end = -1
        for i in range(start, len(text)):
            ch = text[i]
            if ch == '[':
                depth += 1
            elif ch == ']':
                depth -= 1
                if depth == 0:
                    end = i
                    break
        if end > start:
            try:
                out[key] = json.loads(text[start:end + 1])
            except Exception:
                continue
    if not out:
        return None
    # Re-package into the canonical ctx_json shape that callers expect.
    if "interactors" in out and "ctx_json" not in out:
        return {"ctx_json": out, "_salvage_keyed": True}
    return out


def _strategy_brace_balanced(text: str) -> Optional[Any]:
    """Walk the text from each ``{`` and return the longest valid object."""
    best: Optional[Any] = None
    best_len = 0
    for start in range(len(text)):
        if text[start] != '{':
            continue
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(text)):
            ch = text[i]
            if esc:
                esc = False
                continue
            if ch == '\\' and in_str:
                esc = True
                continue
            if ch == '"':
                in_str = not in_str
                continue
            if in_str:
                continue
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    candidate = text[start:i + 1]
                    if len(candidate) <= best_len:
                        break
                    try:
                        parsed = json.loads(candidate)
                        if isinstance(parsed, dict):
                            best = parsed
                            best_len = len(candidate)
                    except Exception:
                        pass
                    break
    return best


_STRATEGIES = (
    ("whole", _strategy_whole),
    ("strip_fences", _strategy_strip_fences),
    ("outer_object", _strategy_outer_object),
    ("outer_array", _strategy_outer_array),
    ("keyed_arrays", _strategy_keyed_arrays),
    ("brace_balanced", _strategy_brace_balanced),
)


def extract_json_from_llm_response(text: str) -> dict:
    """Extract and parse JSON from an LLM response with a 6-strategy salvage chain.

    Logs the rescuing strategy on stderr when a non-trivial fallback fires.
    """
    if not text:
        raise ValueError("Empty LLM response — nothing to parse.")
    for name, fn in _STRATEGIES:
        try:
            result = fn(text)
        except Exception:
            result = None
        if result is None:
            continue
        # Loud log if we needed anything past strategy 1
        if name != "whole":
            print(
                f"[JSON SALVAGE] Strategy '{name}' rescued response "
                f"(length={len(text)} chars).",
                file=sys.stderr,
            )
        if isinstance(result, dict):
            return result
        # Bare-array result is wrapped in _strategy_outer_array; fall back.
        if isinstance(result, list):
            return {"ctx_json": {"interactors": result}, "_salvage_wrapped_array": True}
    raise ValueError(
        f"Failed to parse JSON from LLM response after 6 salvage strategies. "
        f"Length: {len(text)} chars, preview: {text[:200]!r}..."
    )
