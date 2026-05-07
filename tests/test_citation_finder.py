#!/usr/bin/env python3
"""Tests for the Gemini-powered citation finder (utils/citation_finder.py)."""

import json
import sys
from copy import deepcopy
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from utils.citation_finder import (
    ClaimBundle,
    CitationFinderError,
    DailyQuotaExceededError,
    RawCitation,
    VerifiedCitation,
    build_citation_prompt,
    extract_claims,
    find_and_verify_citations,
    parse_gemini_response,
    update_function_evidence,
    verify_pmids_batch,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_fn_block(**overrides):
    """Create a minimal function block."""
    base = {
        "function": "Proteasomal Degradation",
        "arrow": "activates",
        "cellular_process": "VCP binds ATXN3 via the N-terminal domain.",
        "specific_effects": ["2-fold increase in degradation", "Co-IP confirmed"],
        "biological_consequence": ["VCP -> ubiquitination -> degradation"],
        "evidence": [
            {"paper_title": "ATXN3 and VCP in protein quality control", "year": 2020}
        ],
        "pmids": ["12345678"],
    }
    base.update(overrides)
    return base


def _make_payload(interactors=None):
    """Create a minimal pipeline payload."""
    if interactors is None:
        interactors = [
            {
                "primary": "VCP",
                "functions": [_make_fn_block()],
                "pmids": ["12345678"],
            }
        ]
    return {
        "ctx_json": {"main": "ATXN3", "interactors": interactors},
        "snapshot_json": {"main": "ATXN3", "interactors": deepcopy(interactors)},
    }


# ---------------------------------------------------------------------------
# Pure function tests: extract_claims
# ---------------------------------------------------------------------------

class TestExtractClaims:
    def test_basic(self):
        fn = _make_fn_block()
        claim = extract_claims(fn)
        assert claim.function_name == "Proteasomal Degradation"
        assert claim.arrow == "activates"
        assert "VCP binds" in claim.cellular_process
        assert len(claim.specific_effects) == 2
        assert claim.existing_titles == ["ATXN3 and VCP in protein quality control"]
        assert claim.existing_pmids == ["12345678"]

    def test_missing_optional_fields(self):
        fn = {"function": "Autophagy", "arrow": "inhibits"}
        claim = extract_claims(fn)
        assert claim.function_name == "Autophagy"
        assert claim.cellular_process == ""
        assert claim.specific_effects == []
        assert claim.existing_titles == []
        assert claim.existing_pmids == []

    def test_truncates_long_process(self):
        long_text = "A" * 500
        fn = _make_fn_block(cellular_process=long_text)
        claim = extract_claims(fn)
        assert len(claim.cellular_process) == 303  # 300 + "..."
        assert claim.cellular_process.endswith("...")


# ---------------------------------------------------------------------------
# Pure function tests: build_citation_prompt
# ---------------------------------------------------------------------------

class TestBuildCitationPrompt:
    def test_contains_protein_names(self):
        claim = ClaimBundle(
            function_name="Test", arrow="activates",
            cellular_process="mechanism", specific_effects=[],
            existing_titles=[], existing_pmids=[],
        )
        prompt = build_citation_prompt("ATXN3", "VCP", [claim])
        assert "ATXN3" in prompt
        assert "VCP" in prompt

    def test_includes_all_functions(self):
        claims = [
            ClaimBundle("Func A", "activates", "", [], [], []),
            ClaimBundle("Func B", "inhibits", "", [], [], []),
        ]
        prompt = build_citation_prompt("P1", "P2", claims)
        assert "Func A" in prompt
        assert "Func B" in prompt
        assert "FUNCTION 1" in prompt
        assert "FUNCTION 2" in prompt

    def test_includes_existing_title_hints(self):
        claim = ClaimBundle(
            "Test", "activates", "mechanism",
            ["effect1"], ["Some Paper Title"], [],
        )
        prompt = build_citation_prompt("A", "B", [claim])
        assert "Some Paper Title" in prompt

    def test_truncated_process_in_prompt(self):
        claim = ClaimBundle(
            "Test", "activates", "short mechanism",
            [], [], [],
        )
        prompt = build_citation_prompt("A", "B", [claim])
        assert "short mechanism" in prompt


# ---------------------------------------------------------------------------
# Pure function tests: parse_gemini_response
# ---------------------------------------------------------------------------

class TestParseGeminiResponse:
    def test_valid_json(self):
        response = json.dumps({
            "citations": [
                {
                    "function_name": "Test Func",
                    "paper_title": "Real Paper Title",
                    "pmid": 12345678,
                    "year": 2022,
                    "journal": "Nature",
                    "relevant_finding": "Shows interaction",
                }
            ]
        })
        result = parse_gemini_response(response)
        assert len(result) == 1
        assert result[0].function_name == "Test Func"
        assert result[0].pmid == 12345678
        assert result[0].paper_title == "Real Paper Title"

    def test_fenced_json(self):
        response = '```json\n{"citations": [{"function_name": "F", "paper_title": "P", "pmid": 111}]}\n```'
        result = parse_gemini_response(response)
        assert len(result) == 1
        assert result[0].pmid == 111

    def test_empty_string(self):
        assert parse_gemini_response("") == []
        assert parse_gemini_response("   ") == []

    def test_not_found_entries_filtered(self):
        response = json.dumps({
            "citations": [
                {"function_name": "F1", "paper_title": "NOT_FOUND", "pmid": None},
                {"function_name": "F2", "paper_title": "Real Paper", "pmid": 999},
            ]
        })
        result = parse_gemini_response(response)
        assert len(result) == 1
        assert result[0].function_name == "F2"

    def test_null_pmid_handled(self):
        response = json.dumps({
            "citations": [
                {"function_name": "F", "paper_title": "Some Paper", "pmid": None, "year": 2020}
            ]
        })
        result = parse_gemini_response(response)
        assert len(result) == 1
        assert result[0].pmid is None

    def test_malformed_json_fallback(self):
        response = 'Some text before {"citations": [{"function_name": "F", "paper_title": "P"}]} trailing'
        result = parse_gemini_response(response)
        assert len(result) == 1

    def test_completely_invalid(self):
        assert parse_gemini_response("not json at all") == []


# ---------------------------------------------------------------------------
# Pure function tests: update_function_evidence
# ---------------------------------------------------------------------------

class TestUpdateFunctionEvidence:
    def test_replaces_evidence_with_verified(self):
        fn = _make_fn_block()
        verified = [
            VerifiedCitation(
                function_name="Proteasomal Degradation",
                pmid="99999999",
                canonical_title="Verified Paper Title",
                year=2023,
                journal="Cell",
                relevant_finding="Confirms interaction",
            )
        ]
        update_function_evidence(fn, verified)
        assert len(fn["evidence"]) == 1
        assert fn["evidence"][0]["pmid"] == "99999999"
        assert fn["evidence"][0]["paper_title"] == "Verified Paper Title"
        assert fn["pmids"] == ["99999999"]
        assert "_citation_status" not in fn

    def test_preserves_originals_on_empty(self):
        fn = _make_fn_block()
        original_evidence = fn["evidence"][:]
        update_function_evidence(fn, [])
        assert fn["evidence"] == original_evidence
        assert fn["_citation_status"] == "unresolved"

    def test_never_deletes_function(self):
        fn = _make_fn_block()
        update_function_evidence(fn, [])
        assert "function" in fn
        assert fn["function"] == "Proteasomal Degradation"

    def test_deduplicates_pmids(self):
        fn = _make_fn_block()
        verified = [
            VerifiedCitation("F", "111", "Paper A", 2020, "J1", "Finding A"),
            VerifiedCitation("F", "111", "Paper A duplicate", 2020, "J1", "Finding A2"),
            VerifiedCitation("F", "222", "Paper B", 2021, "J2", "Finding B"),
        ]
        update_function_evidence(fn, verified)
        assert fn["pmids"] == ["111", "222"]
        assert len(fn["evidence"]) == 3  # all entries kept, pmids deduped

    def test_clears_unresolved_status_on_success(self):
        fn = _make_fn_block()
        fn["_citation_status"] = "unresolved"
        verified = [
            VerifiedCitation("F", "111", "Paper", 2020, "J", "Finding"),
        ]
        update_function_evidence(fn, verified)
        assert "_citation_status" not in fn


# ---------------------------------------------------------------------------
# Mocked tests: verify_pmids_batch
# ---------------------------------------------------------------------------

class TestVerifyPmidsBatch:
    def test_confirms_valid_pmids(self):
        raw = [
            RawCitation("F", "Real Paper", 12345678, 2022, "Nature", "Finding"),
        ]
        mock_client = MagicMock()
        mock_client.fetch_titles.return_value = {"12345678": "Canonical Title From NCBI"}

        result = verify_pmids_batch(raw, mock_client)
        assert len(result) == 1
        assert result[0].pmid == "12345678"
        assert result[0].canonical_title == "Canonical Title From NCBI"

    def test_fallback_title_search_on_missing_pmid(self):
        raw = [
            RawCitation("F", "Some Paper About ATXN3", 99999999, 2022, "J", "Finding"),
        ]
        mock_client = MagicMock()
        # PMID not found in batch fetch
        mock_client.fetch_titles.side_effect = [
            {},  # first call: batch fetch returns empty
            {"11111111": "Some Paper About ATXN3 And VCP"},  # second call: title search results
        ]
        mock_client.search_ids.return_value = ["11111111"]

        result = verify_pmids_batch(raw, mock_client)
        assert len(result) == 1
        assert result[0].pmid == "11111111"

    def test_drops_unverifiable_citations(self):
        raw = [
            RawCitation("F", "Fake Paper", 88888888, 2022, "J", "Finding"),
        ]
        mock_client = MagicMock()
        mock_client.fetch_titles.return_value = {}
        mock_client.search_ids.return_value = []

        result = verify_pmids_batch(raw, mock_client)
        assert len(result) == 0

    def test_empty_input(self):
        result = verify_pmids_batch([], MagicMock())
        assert result == []

    def test_null_pmid_triggers_title_search(self):
        raw = [
            RawCitation("F", "Known Paper Title", None, 2022, "J", "Finding"),
        ]
        mock_client = MagicMock()
        mock_client.fetch_titles.return_value = {"55555555": "Known Paper Title"}
        mock_client.search_ids.return_value = ["55555555"]

        result = verify_pmids_batch(raw, mock_client)
        assert len(result) == 1
        assert result[0].pmid == "55555555"


# ---------------------------------------------------------------------------
# Mocked tests: find_and_verify_citations (end-to-end)
# ---------------------------------------------------------------------------

class TestFindAndVerifyCitations:
    @patch("utils.citation_finder.call_gemini_citation_search")
    @patch("utils.citation_finder.PubMedClient")
    def test_end_to_end(self, MockPubMedClient, mock_gemini):
        mock_gemini.return_value = [
            RawCitation("Proteasomal Degradation", "Real Paper", 11111111, 2022, "Nature", "Shows interaction"),
        ]
        mock_ncbi = MockPubMedClient.return_value
        mock_ncbi.fetch_titles.return_value = {"11111111": "Real Paper From NCBI"}
        mock_ncbi.search_ids.return_value = []

        payload = _make_payload()
        result = find_and_verify_citations(payload, "test-key", verbose=False)

        fn = result["ctx_json"]["interactors"][0]["functions"][0]
        assert fn["evidence"][0]["pmid"] == "11111111"
        assert fn["pmids"] == ["11111111"]
        assert "_citation_status" not in fn

    @patch("utils.citation_finder.call_gemini_citation_search")
    @patch("utils.citation_finder.PubMedClient")
    def test_updates_snapshot_json(self, MockPubMedClient, mock_gemini):
        mock_gemini.return_value = [
            RawCitation("Proteasomal Degradation", "Paper", 22222222, 2023, "Cell", "Finding"),
        ]
        mock_ncbi = MockPubMedClient.return_value
        mock_ncbi.fetch_titles.return_value = {"22222222": "Paper Title"}

        payload = _make_payload()
        result = find_and_verify_citations(payload, "test-key", verbose=False)

        # snapshot should be updated independently
        snap_fn = result["snapshot_json"]["interactors"][0]["functions"][0]
        assert snap_fn["evidence"][0]["pmid"] == "22222222"

        # verify independence
        result["ctx_json"]["interactors"][0]["_mutate"] = True
        assert "_mutate" not in result["snapshot_json"]["interactors"][0]

    @patch("utils.citation_finder.call_gemini_citation_search")
    @patch("utils.citation_finder.PubMedClient")
    def test_no_api_key_skips(self, MockPubMedClient, mock_gemini):
        payload = _make_payload()
        result = find_and_verify_citations(payload, None, verbose=False)
        mock_gemini.assert_not_called()
        # payload unchanged
        assert result["ctx_json"]["interactors"][0]["functions"][0]["pmids"] == ["12345678"]

    @patch("utils.citation_finder.call_gemini_citation_search")
    @patch("utils.citation_finder.PubMedClient")
    def test_quota_stop_marks_remaining(self, MockPubMedClient, mock_gemini):
        mock_gemini.side_effect = DailyQuotaExceededError("quota hit")

        interactors = [
            {"primary": "VCP", "functions": [_make_fn_block()]},
            {"primary": "HDAC6", "functions": [_make_fn_block(function="Deacetylation")]},
        ]
        payload = _make_payload(interactors)
        result = find_and_verify_citations(payload, "test-key", verbose=False)

        # Both should be marked
        for inter in result["ctx_json"]["interactors"]:
            for fn in inter["functions"]:
                assert fn["_citation_status"] in ("skipped_quota", "gemini_error")

    @patch("utils.citation_finder.call_gemini_citation_search")
    @patch("utils.citation_finder.PubMedClient")
    def test_gemini_error_marks_unresolved(self, MockPubMedClient, mock_gemini):
        mock_gemini.side_effect = CitationFinderError("API error")

        payload = _make_payload()
        result = find_and_verify_citations(payload, "test-key", verbose=False)

        fn = result["ctx_json"]["interactors"][0]["functions"][0]
        assert fn["_citation_status"] == "gemini_error"
        # original evidence preserved
        assert fn["evidence"][0]["paper_title"] == "ATXN3 and VCP in protein quality control"

    @patch("utils.citation_finder.call_gemini_citation_search")
    @patch("utils.citation_finder.PubMedClient")
    def test_tracks_metrics(self, MockPubMedClient, mock_gemini):
        mock_gemini.return_value = [
            RawCitation("Proteasomal Degradation", "Paper", 33333333, 2023, "J", "F"),
        ]
        mock_ncbi = MockPubMedClient.return_value
        mock_ncbi.fetch_titles.return_value = {"33333333": "Paper Title"}

        payload = _make_payload()
        result = find_and_verify_citations(payload, "test-key", verbose=False)

        metrics = result.get("_request_metrics", {})
        assert "citation_gemini_calls" in metrics
        assert "citation_ncbi_calls" in metrics
        assert "citation_verified_count" in metrics


# ---------------------------------------------------------------------------
# Property test: functions are NEVER deleted
# ---------------------------------------------------------------------------

class TestFunctionsNeverDeleted:
    @patch("utils.citation_finder.call_gemini_citation_search")
    @patch("utils.citation_finder.PubMedClient")
    def test_function_count_preserved(self, MockPubMedClient, mock_gemini):
        """No matter what happens, function count must never decrease."""
        mock_gemini.return_value = []  # Gemini finds nothing
        mock_ncbi = MockPubMedClient.return_value
        mock_ncbi.fetch_titles.return_value = {}

        fns = [
            _make_fn_block(function="Func A"),
            _make_fn_block(function="Func B"),
            _make_fn_block(function="Func C"),
        ]
        interactors = [{"primary": "VCP", "functions": fns, "pmids": []}]
        payload = _make_payload(interactors)

        count_before = len(payload["ctx_json"]["interactors"][0]["functions"])
        result = find_and_verify_citations(payload, "test-key", verbose=False)
        count_after = len(result["ctx_json"]["interactors"][0]["functions"])

        assert count_after == count_before  # never deleted


# ---------------------------------------------------------------------------
# Integration: PostProcessor stage wiring
# ---------------------------------------------------------------------------

class TestPostProcessorIntegration:
    def test_stage_wired_correctly(self):
        from utils.post_processor import PostProcessor, StageKind
        stages = PostProcessor.default_stages()
        pmid_stage = next((s for s in stages if s.name == "update_pmids"), None)
        assert pmid_stage is not None
        assert pmid_stage.kind == StageKind.LLM
        assert pmid_stage.requires_api_key is True
        assert pmid_stage.skip_flag == "skip_pmid_update"

    def test_requery_stage_wired(self):
        from utils.post_processor import PostProcessor, StageKind
        stages = PostProcessor.requery_stages()
        pmid_stage = next((s for s in stages if s.name == "update_pmids"), None)
        assert pmid_stage is not None
        assert pmid_stage.kind == StageKind.LLM
        assert pmid_stage.requires_api_key is True
