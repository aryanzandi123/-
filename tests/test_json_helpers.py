"""Unit tests for utils/json_helpers.py."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import json
import pytest

from utils.json_helpers import (
    PipelineError,
    deep_merge_interactors,
    parse_json_output,
    repair_truncated_json,
    strip_code_fences,
)


# ── strip_code_fences ────────────────────────────────────────────────────


class TestStripCodeFences:
    def test_json_fences_removed(self):
        raw = '```json\n{"a": 1}\n```'
        assert strip_code_fences(raw) == '{"a": 1}'

    def test_plain_fences_removed(self):
        raw = "```\nhello world\n```"
        assert strip_code_fences(raw) == "hello world"

    def test_csv_fences_removed(self):
        raw = "```csv\na,b,c\n1,2,3\n```"
        assert strip_code_fences(raw) == "a,b,c\n1,2,3"

    def test_no_fences_passthrough(self):
        raw = '{"a": 1}'
        assert strip_code_fences(raw) == '{"a": 1}'

    def test_none_input_returns_empty_string(self):
        assert strip_code_fences(None) == ""


# ── deep_merge_interactors ───────────────────────────────────────────────


class TestDeepMergeInteractors:
    def test_new_interactor_added(self):
        existing = [{"primary": "TP53", "functions": []}]
        new = [{"primary": "BRCA1", "functions": []}]
        result = deep_merge_interactors(existing, new)
        primaries = [i["primary"] for i in result]
        assert "TP53" in primaries
        assert "BRCA1" in primaries

    def test_existing_interactor_functions_merged(self):
        existing = [
            {
                "primary": "TP53",
                "functions": [
                    {"function": "apoptosis", "cellular_process": "cell death", "direction": "main_to_primary"},
                ],
            }
        ]
        new = [
            {
                "primary": "TP53",
                "functions": [
                    {"function": "cell cycle arrest", "cellular_process": "growth", "direction": "main_to_primary"},
                ],
            }
        ]
        result = deep_merge_interactors(existing, new)
        tp53 = [i for i in result if i["primary"] == "TP53"][0]
        fn_names = [f["function"] for f in tp53["functions"]]
        assert "apoptosis" in fn_names
        assert "cell cycle arrest" in fn_names

    def test_duplicate_function_updated_not_appended(self):
        existing = [
            {
                "primary": "TP53",
                "functions": [
                    {
                        "function": "apoptosis",
                        "cellular_process": "cell death",
                        "direction": "main_to_primary",
                        "arrow": "activates",
                    },
                ],
            }
        ]
        new = [
            {
                "primary": "TP53",
                "functions": [
                    {
                        "function": "apoptosis",
                        "cellular_process": "cell death",
                        "direction": "main_to_primary",
                        "arrow": "inhibits",
                    },
                ],
            }
        ]
        result = deep_merge_interactors(existing, new)
        tp53 = [i for i in result if i["primary"] == "TP53"][0]
        # Should have exactly one function (updated, not duplicated)
        assert len(tp53["functions"]) == 1
        assert tp53["functions"][0]["arrow"] == "inhibits"

    def test_pmids_merged_as_union(self):
        existing = [
            {
                "primary": "TP53",
                "functions": [
                    {
                        "function": "apoptosis",
                        "cellular_process": "cell death",
                        "direction": "main_to_primary",
                        "pmids": ["123", "456"],
                    },
                ],
            }
        ]
        new = [
            {
                "primary": "TP53",
                "functions": [
                    {
                        "function": "apoptosis",
                        "cellular_process": "cell death",
                        "direction": "main_to_primary",
                        "pmids": ["456", "789"],
                    },
                ],
            }
        ]
        result = deep_merge_interactors(existing, new)
        tp53 = [i for i in result if i["primary"] == "TP53"][0]
        pmids = set(tp53["functions"][0]["pmids"])
        assert pmids == {"123", "456", "789"}

    def test_interaction_type_preserved_from_existing(self):
        existing = [
            {
                "primary": "TP53",
                "interaction_type": "direct",
                "functions": [],
            }
        ]
        new = [
            {
                "primary": "TP53",
                "interaction_type": "indirect",
                "functions": [],
            }
        ]
        result = deep_merge_interactors(existing, new)
        tp53 = [i for i in result if i["primary"] == "TP53"][0]
        # Phase 1 (existing) interaction_type is authoritative
        assert tp53["interaction_type"] == "direct"

    # ── H4: chain_link_functions cross-batch merge ─────────────────────

    def test_chain_link_functions_richer_text_wins(self):
        """When two batches produce the same function name + context for
        the same pair, the longer cellular_process must win — the
        previous merge silently dropped the second entry's richer text."""
        existing = [
            {
                "primary": "MTOR",
                "chain_link_functions": {
                    "ATXN3->MTOR": [
                        {
                            "function": "Activates mTORC1",
                            "function_context": "chain_derived",
                            "cellular_process": "short text",
                            "evidence": [],
                            "pmids": ["1"],
                        }
                    ]
                },
            }
        ]
        new = [
            {
                "primary": "MTOR",
                "chain_link_functions": {
                    "ATXN3->MTOR": [
                        {
                            "function": "Activates mTORC1",
                            "function_context": "chain_derived",
                            "cellular_process": (
                                "much longer text describing binding "
                                "domains, residues, and PTM context"
                            ),
                            "evidence": [{"pmid": "2", "paper_title": "P2"}],
                            "pmids": ["2"],
                        }
                    ]
                },
            }
        ]
        result = deep_merge_interactors(existing, new)
        mtor = [i for i in result if i["primary"] == "MTOR"][0]
        # Canonical pair key is "ATXN3|MTOR"
        funcs = mtor["chain_link_functions"]["ATXN3|MTOR"]
        assert len(funcs) == 1, "same name+context must collapse, not duplicate"
        assert "longer text" in funcs[0]["cellular_process"], (
            "richer text from the second batch must survive"
        )
        assert sorted(funcs[0]["pmids"]) == ["1", "2"]
        assert len(funcs[0]["evidence"]) == 1

    def test_chain_link_functions_different_context_keep_separate(self):
        """A 'direct' and a 'chain_derived' entry sharing the same name
        must NOT merge — they're different rows under the schema's
        uq_claim_interaction_fn_pw_ctx index."""
        existing = [
            {
                "primary": "MTOR",
                "chain_link_functions": {
                    "ATXN3->MTOR": [
                        {
                            "function": "Activates mTORC1",
                            "function_context": "direct",
                            "cellular_process": "direct text",
                            "evidence": [], "pmids": [],
                        }
                    ]
                },
            }
        ]
        new = [
            {
                "primary": "MTOR",
                "chain_link_functions": {
                    "ATXN3->MTOR": [
                        {
                            "function": "Activates mTORC1",
                            "function_context": "chain_derived",
                            "cellular_process": "chain text",
                            "evidence": [], "pmids": [],
                        }
                    ]
                },
            }
        ]
        result = deep_merge_interactors(existing, new)
        mtor = [i for i in result if i["primary"] == "MTOR"][0]
        funcs = mtor["chain_link_functions"]["ATXN3|MTOR"]
        contexts = sorted(f["function_context"] for f in funcs)
        assert contexts == ["chain_derived", "direct"], (
            "different function_context must NEVER collapse, "
            f"got {contexts}"
        )

    def test_chain_link_functions_merge_idempotent(self):
        """Running the merge twice with the same inputs must produce the
        same output as running it once. Catches the crash-save retry
        bug where re-emitting a batch could lose data."""
        a = [
            {
                "primary": "MTOR",
                "chain_link_functions": {
                    "ATXN3->MTOR": [
                        {
                            "function": "fn1",
                            "function_context": "chain_derived",
                            "cellular_process": "version A",
                            "pmids": ["1"], "evidence": [],
                        }
                    ]
                },
            }
        ]
        b = [
            {
                "primary": "MTOR",
                "chain_link_functions": {
                    "ATXN3->MTOR": [
                        {
                            "function": "fn1",
                            "function_context": "chain_derived",
                            "cellular_process": "version B is longer",
                            "pmids": ["2"], "evidence": [],
                        }
                    ]
                },
            }
        ]
        once = deep_merge_interactors(a, b)
        # Re-running the same merge on the result must NOT change it.
        twice = deep_merge_interactors(once, b)

        once_funcs = once[0]["chain_link_functions"]["ATXN3|MTOR"]
        twice_funcs = twice[0]["chain_link_functions"]["ATXN3|MTOR"]
        assert len(once_funcs) == len(twice_funcs) == 1
        assert sorted(once_funcs[0]["pmids"]) == sorted(twice_funcs[0]["pmids"]) == ["1", "2"]
        assert once_funcs[0]["cellular_process"] == twice_funcs[0]["cellular_process"]
        assert "version B" in once_funcs[0]["cellular_process"], (
            "longer text must survive both passes"
        )


# ── parse_json_output ────────────────────────────────────────────────────


class TestParseJsonOutput:
    def test_valid_json_parsed(self):
        text = '{"ctx_json": {"main": "TP53"}, "step_json": {}}'
        result = parse_json_output(text, ["ctx_json", "step_json"])
        assert result["ctx_json"]["main"] == "TP53"

    def test_missing_required_field_raises(self):
        text = '{"ctx_json": {"main": "TP53"}}'
        with pytest.raises(PipelineError, match="Missing required fields"):
            parse_json_output(text, ["ctx_json", "step_json"])

    def test_empty_input_raises(self):
        with pytest.raises(PipelineError, match="Empty or null"):
            parse_json_output("", ["ctx_json"])

    def test_none_input_raises(self):
        with pytest.raises(PipelineError, match="Empty or null"):
            parse_json_output(None, ["ctx_json"])

    def test_code_fences_stripped_before_parse(self):
        text = '```json\n{"ctx_json": {"main": "TP53"}, "step_json": {}}\n```'
        result = parse_json_output(text, ["ctx_json", "step_json"])
        assert result["ctx_json"]["main"] == "TP53"


# ── repair_truncated_json ─────────────────────────────────────────────────


class TestRepairTruncatedJson:
    def test_repair_truncated_string(self):
        result = repair_truncated_json('{"k": "val')
        parsed = json.loads(result)
        assert parsed["k"] == "val"

    def test_repair_truncated_nested(self):
        text = '{"ctx_json": {"main": "ATXN3", "interactors": [{"primary": "HDAC3"'
        result = repair_truncated_json(text)
        parsed = json.loads(result)
        assert parsed["ctx_json"]["main"] == "ATXN3"
        assert parsed["ctx_json"]["interactors"][0]["primary"] == "HDAC3"

    def test_complete_json_unchanged(self):
        text = '{"a": 1, "b": [2, 3]}'
        assert repair_truncated_json(text) == text

    def test_empty_input(self):
        assert repair_truncated_json("") == ""

    def test_no_braces(self):
        assert repair_truncated_json("hello world") == "hello world"

    def test_repair_trailing_comma(self):
        text = '{"items": [1, 2,'
        result = repair_truncated_json(text)
        parsed = json.loads(result)
        assert parsed["items"] == [1, 2]

    def test_repair_with_garbage_prefix_via_parse(self):
        """Exact failure case from the bug: garbage prefix + truncated JSON.

        repair_truncated_json alone cannot handle a garbage prefix with
        unterminated strings, but parse_json_output's fallback tries repair
        from each '{' position, which succeeds.
        """
        text = (
            '[], "depth": 1, "direction": "main_to_\n\n'
            '{"ctx_json": {"main": "ATXN3", "interactors": [{"primary": "HDAC3"'
        )
        # Direct repair on a substring starting at the JSON object works:
        substring = text[text.index('{"ctx_json"'):]
        repaired = repair_truncated_json(substring)
        parsed = json.loads(repaired)
        assert parsed["ctx_json"]["main"] == "ATXN3"

    def test_parse_json_output_garbage_prefix_recovery(self):
        """End-to-end: parse_json_output recovers from garbage prefix + truncated JSON."""
        text = (
            '[], "depth": 1, "direction": "main_to_\n\n'
            '{"ctx_json": {"main": "ATXN3", "interactors": []}, "step_json": {"x": 1'
        )
        result = parse_json_output(text, ["ctx_json", "step_json"])
        assert result["ctx_json"]["main"] == "ATXN3"


class TestParseJsonOutputTruncatedFallback:
    def test_truncated_json_recovered(self):
        """parse_json_output should recover truncated JSON via repair."""
        text = '{"ctx_json": {"main": "VCP", "interactors": []}, "step_json": {"status": "ok"'
        result = parse_json_output(text, ["ctx_json", "step_json"])
        assert result["ctx_json"]["main"] == "VCP"

    def test_truncated_ctx_json_only_with_previous_payload(self):
        """Truncated JSON with ctx_json but no step_json, merged with previous_payload."""
        text = (
            '[], "depth": 1, "direction": "main_to_\n\n'
            '{"ctx_json": {"main": "ATXN3", "interactors": [{"primary": "HDAC3"'
        )
        prev = {"ctx_json": {"main": "ATXN3", "interactors": []}, "step_json": {"step": "prev"}}
        result = parse_json_output(text, ["ctx_json", "step_json"], previous_payload=prev)
        assert result["ctx_json"]["main"] == "ATXN3"
        # step_json survives from previous_payload
        assert "step_json" in result
        # Recovered interactor is merged
        primaries = [i["primary"] for i in result["ctx_json"]["interactors"]]
        assert "HDAC3" in primaries

    def test_truncated_ctx_json_only_no_previous_payload(self):
        """Truncated JSON with ctx_json but no step_json and no previous_payload raises missing fields."""
        text = (
            '[], "depth": 1, "direction": "main_to_\n\n'
            '{"ctx_json": {"main": "ATXN3", "interactors": [{"primary": "HDAC3"'
        )
        with pytest.raises(PipelineError, match="Missing required fields"):
            parse_json_output(text, ["ctx_json", "step_json"])

    def test_garbage_brace_before_real_data(self):
        """Garbage JSON object before real ctx_json data; should recover real data."""
        text = (
            '{"garbage": true} some text '
            '{"ctx_json": {"main": "VCP", "interactors": []}, "step_json": {"x": 1'
        )
        result = parse_json_output(text, ["ctx_json", "step_json"])
        assert result["ctx_json"]["main"] == "VCP"

    def test_enhanced_error_message(self):
        """When no JSON dicts are found at all, error includes diagnostics."""
        with pytest.raises(PipelineError, match=r"Length: \d+ chars"):
            parse_json_output("no json here at all", ["ctx_json"])
