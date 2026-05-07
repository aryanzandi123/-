"""Tests for the iterative research pipeline (Gemini 3.1 Pro multi-iteration discovery)."""
from __future__ import annotations

import json
import pytest
from unittest.mock import MagicMock, patch

from pipeline.types import IterationConfig, StepConfig


# ═══════════════════════════════════════════════════════════════
# IterationConfig dataclass
# ═══════════════════════════════════════════════════════════════


class TestIterationConfig:
    def test_valid_creation(self):
        cfg = IterationConfig(
            name="broad", focus="Broad discovery", prompt_template="Find {user_query}"
        )
        assert cfg.name == "broad"
        assert cfg.focus == "Broad discovery"
        assert cfg.search_queries_hint == ()

    def test_with_search_hints(self):
        cfg = IterationConfig(
            name="broad",
            focus="Broad",
            prompt_template="Find {user_query}",
            search_queries_hint=("{user_query} interactions",),
        )
        assert len(cfg.search_queries_hint) == 1

    def test_empty_name_raises(self):
        with pytest.raises(ValueError, match="name cannot be empty"):
            IterationConfig(name="", focus="test", prompt_template="test")

    def test_empty_prompt_raises(self):
        with pytest.raises(ValueError, match="prompt_template"):
            IterationConfig(name="test", focus="test", prompt_template="   ")

    def test_frozen(self):
        cfg = IterationConfig(name="test", focus="test", prompt_template="test")
        with pytest.raises(AttributeError):
            cfg.name = "changed"


# ═══════════════════════════════════════════════════════════════
# StepConfig iteration_configs field
# ═══════════════════════════════════════════════════════════════


class TestStepConfigIterationField:
    def test_default_is_none(self):
        step = StepConfig(
            name="test",
            model="gemini-3.1-pro-preview",
            prompt_template="test",
            expected_columns=["ctx_json"],
        )
        assert step.iteration_configs is None

    def test_with_iteration_configs(self):
        cfg = IterationConfig(name="broad", focus="Broad", prompt_template="test")
        step = StepConfig(
            name="test",
            model="gemini-3.1-pro-preview",
            prompt_template="test",
            expected_columns=["ctx_json"],
            api_mode="iterative_research",
            iteration_configs=(cfg,),
        )
        assert len(step.iteration_configs) == 1
        assert step.iteration_configs[0].name == "broad"


# ═══════════════════════════════════════════════════════════════
# build_default_iteration_configs
# ═══════════════════════════════════════════════════════════════


class TestBuildDefaultIterationConfigs:
    def test_default_returns_6(self):
        from pipeline.prompts.iterative_research_steps import build_default_iteration_configs
        configs = build_default_iteration_configs()
        assert len(configs) == 6

    def test_custom_count(self):
        from pipeline.prompts.iterative_research_steps import build_default_iteration_configs
        assert len(build_default_iteration_configs(3)) == 3
        assert len(build_default_iteration_configs(1)) == 1

    def test_clamped_low(self):
        from pipeline.prompts.iterative_research_steps import build_default_iteration_configs
        assert len(build_default_iteration_configs(0)) == 1

    def test_clamped_high(self):
        from pipeline.prompts.iterative_research_steps import build_default_iteration_configs
        assert len(build_default_iteration_configs(100)) == 7

    def test_all_have_nonempty_fields(self):
        from pipeline.prompts.iterative_research_steps import build_default_iteration_configs
        for cfg in build_default_iteration_configs():
            assert cfg.name
            assert cfg.focus
            assert cfg.prompt_template.strip()


# ═══════════════════════════════════════════════════════════════
# step1_iterative_research_discovery factory
# ═══════════════════════════════════════════════════════════════


class TestStep1Factory:
    def test_produces_valid_step_config(self):
        from pipeline.prompts.iterative_research_steps import step1_iterative_research_discovery
        step = step1_iterative_research_discovery()
        assert step.name == "step1_iterative_research_discovery"
        assert step.api_mode == "iterative_research"
        assert step.iteration_configs is not None
        assert len(step.iteration_configs) == 6

    def test_custom_iteration_count(self):
        from pipeline.prompts.iterative_research_steps import step1_iterative_research_discovery
        step = step1_iterative_research_discovery(num_iterations=3)
        assert len(step.iteration_configs) == 3

    def test_model_is_iterative(self):
        from pipeline.prompts.iterative_research_steps import step1_iterative_research_discovery
        step = step1_iterative_research_discovery()
        # Should use gemini-3.1-pro-preview (or env override)
        assert "gemini" in step.model.lower()


# ═══════════════════════════════════════════════════════════════
# generate_iterative_pipeline
# ═══════════════════════════════════════════════════════════════


class TestGenerateIterativePipeline:
    def test_default_step_count(self):
        from pipeline.config_dynamic import generate_iterative_pipeline
        steps = generate_iterative_pipeline()
        # 1 iterative + 1 step2a + 3 visible chain stages + 1 step2e + 1 snapshot.
        assert len(steps) == 7

    def test_all_names_unique(self):
        from pipeline.config_dynamic import generate_iterative_pipeline
        steps = generate_iterative_pipeline()
        names = [s.name for s in steps]
        assert len(names) == len(set(names))

    def test_first_step_is_iterative(self):
        from pipeline.config_dynamic import generate_iterative_pipeline
        steps = generate_iterative_pipeline()
        assert steps[0].api_mode == "iterative_research"
        assert steps[0].name == "step1_iterative_research_discovery"

    def test_custom_function_rounds(self):
        from pipeline.config_dynamic import generate_iterative_pipeline
        steps = generate_iterative_pipeline(num_function_rounds=1)
        # num_function_rounds doesn't affect count — always 1 step2a marker
        # 1 iterative + 1 step2a + 3 visible chain stages + 1 step2e + 1 snapshot.
        assert len(steps) == 7

    def test_discovery_iterations_passed(self):
        from pipeline.config_dynamic import generate_iterative_pipeline
        steps = generate_iterative_pipeline(discovery_iterations=3)
        assert len(steps[0].iteration_configs) == 3

    def test_clamping(self):
        from pipeline.config_dynamic import generate_iterative_pipeline
        steps = generate_iterative_pipeline(discovery_iterations=0)
        assert len(steps[0].iteration_configs) == 1
        steps = generate_iterative_pipeline(discovery_iterations=100)
        assert len(steps[0].iteration_configs) == 7  # all defined defaults

    def test_no_combined_step(self):
        from pipeline.config_dynamic import generate_iterative_pipeline
        steps = generate_iterative_pipeline()
        names = [s.name for s in steps]
        assert "step2b_deep_functions_combined" not in names


# ═══════════════════════════════════════════════════════════════
# _get_all_indirect_interactors
# ═══════════════════════════════════════════════════════════════


class TestGetAllIndirectInteractors:
    def test_returns_all_indirect(self):
        from runner import _get_all_indirect_interactors
        ctx = {
            "interactors": [
                {"primary": "VCP", "interaction_type": "direct", "functions": [{"function": "x"}]},
                {"primary": "LAMP2", "interaction_type": "indirect", "functions": [{"function": "y"}]},
                {"primary": "mTOR", "interaction_type": "indirect", "functions": []},
                {"primary": "ATG5", "interaction_type": "indirect"},
            ]
        }
        result = _get_all_indirect_interactors(ctx)
        # Should return ALL indirect, including LAMP2 which already has functions
        assert set(result) == {"LAMP2", "mTOR", "ATG5"}

    def test_returns_empty_for_no_indirect(self):
        from runner import _get_all_indirect_interactors
        ctx = {
            "interactors": [
                {"primary": "VCP", "interaction_type": "direct"},
            ]
        }
        assert _get_all_indirect_interactors(ctx) == []

    def test_skips_entries_without_primary(self):
        from runner import _get_all_indirect_interactors
        ctx = {
            "interactors": [
                {"interaction_type": "indirect"},
                {"primary": "LAMP2", "interaction_type": "indirect"},
            ]
        }
        assert _get_all_indirect_interactors(ctx) == ["LAMP2"]


# ═══════════════════════════════════════════════════════════════
# _promote_discovered_interactors
# ═══════════════════════════════════════════════════════════════


class TestPromoteDiscoveredInteractors:
    def test_promotes_from_indirect_interactors_array(self):
        from runner import _promote_discovered_interactors
        payload = {"ctx_json": {
            "main": "ATXN3",
            "interactors": [
                {"primary": "VCP", "interaction_type": "direct", "functions": []},
            ],
            "interactor_history": ["VCP"],
            "indirect_interactors": [
                {"name": "mTOR", "upstream_interactor": "VCP",
                 "discovered_in_function": "test", "role_in_cascade": "downstream"},
            ],
        }}
        updated, promoted = _promote_discovered_interactors(payload)
        assert "mTOR" in promoted
        names = [i["primary"] for i in updated["ctx_json"]["interactors"]]
        assert "mTOR" in names
        mtor = next(i for i in updated["ctx_json"]["interactors"] if i["primary"] == "mTOR")
        assert mtor["interaction_type"] == "indirect"
        assert mtor["upstream_interactor"] == "VCP"

    def test_skips_existing_interactors(self):
        from runner import _promote_discovered_interactors
        payload = {"ctx_json": {
            "main": "ATXN3",
            "interactors": [
                {"primary": "VCP", "interaction_type": "direct", "functions": []},
            ],
            "indirect_interactors": [
                {"name": "VCP", "upstream_interactor": "ATXN3"},
            ],
        }}
        _, promoted = _promote_discovered_interactors(payload)
        assert promoted == []

    def test_skips_main_protein(self):
        from runner import _promote_discovered_interactors
        payload = {"ctx_json": {
            "main": "ATXN3",
            "interactors": [],
            "indirect_interactors": [
                {"name": "ATXN3", "upstream_interactor": "VCP"},
            ],
        }}
        _, promoted = _promote_discovered_interactors(payload)
        assert promoted == []

    def test_reclassifies_direct_to_indirect(self):
        from runner import _promote_discovered_interactors
        payload = {"ctx_json": {
            "main": "ATXN3",
            "interactors": [
                {"primary": "NPLOC4", "interaction_type": "direct", "functions": [
                    {"function": "ERAD", "_evidence_suggests_indirect": True,
                     "_implicated_mediators": ["VCP"]},
                ]},
            ],
        }}
        updated, _ = _promote_discovered_interactors(updated if False else payload)
        nploc4 = next(
            i for i in updated["ctx_json"]["interactors"] if i["primary"] == "NPLOC4"
        )
        assert nploc4["interaction_type"] == "indirect"
        assert nploc4["upstream_interactor"] == "VCP"

    def test_promotes_implicated_proteins_from_functions(self):
        from runner import _promote_discovered_interactors
        payload = {"ctx_json": {
            "main": "ATXN3",
            "interactors": [
                {"primary": "NPLOC4", "interaction_type": "direct", "functions": [
                    {"function": "ERAD", "_implicated_proteins": ["UFD1", "VCP"]},
                ]},
            ],
            "interactor_history": ["NPLOC4"],
        }}
        updated, promoted = _promote_discovered_interactors(payload)
        assert "UFD1" in promoted
        assert "VCP" in promoted
        ufd1 = next(i for i in updated["ctx_json"]["interactors"] if i["primary"] == "UFD1")
        assert ufd1["upstream_interactor"] == "NPLOC4"

    def test_empty_indirect_interactors(self):
        from runner import _promote_discovered_interactors
        payload = {"ctx_json": {
            "main": "ATXN3",
            "interactors": [{"primary": "VCP", "interaction_type": "direct"}],
        }}
        _, promoted = _promote_discovered_interactors(payload)
        assert promoted == []

    def test_implicated_proteins_processed_first(self):
        """_implicated_proteins (with chain data) should win over indirect_interactors (without)."""
        from runner import _promote_discovered_interactors
        payload = {"ctx_json": {
            "main": "ATXN3",
            "interactors": [
                {"primary": "TP53", "interaction_type": "direct", "functions": [
                    {"function": "test", "_implicated_proteins": ["BCL2"]},
                ]},
            ],
            "interactor_history": ["TP53"],
            "indirect_interactors": [
                {"name": "BCL2"},  # No upstream — would create empty chain
            ],
        }}
        updated, promoted = _promote_discovered_interactors(payload)
        bcl2 = next(i for i in updated["ctx_json"]["interactors"] if i["primary"] == "BCL2")
        # Should have chain data from _implicated_proteins (TP53), not empty from indirect_interactors
        assert bcl2["mediator_chain"] == ["TP53"]
        assert bcl2["upstream_interactor"] == "TP53"


# ═══════════════════════════════════════════════════════════════
# _reconcile_chain_fields
# ═══════════════════════════════════════════════════════════════


class TestReconcileChainFields:
    """The reconciler is a one-way pipe from canonical ``full_chain`` to the
    denormalised columns. No more upstream-salvage, no more function _context
    reconstruction — those hid prompt drift. Missing full_chain is a bug, and
    the function logs it loudly instead of silently papering over it.
    """

    def test_reconciles_from_full_chain_query_at_head(self):
        from runner import _reconcile_chain_fields
        payload = {"ctx_json": {
            "main": "ATXN3",
            "interactors": [
                {"primary": "BCL2", "interaction_type": "indirect",
                 "chain_context": {
                     "full_chain": ["ATXN3", "TP53", "MDM2", "BCL2"],
                     "query_protein": "ATXN3",
                     "query_position": 0,
                     "chain_length": 4,
                 }},
            ]
        }}
        result = _reconcile_chain_fields(payload)
        bcl2 = result["ctx_json"]["interactors"][0]
        assert bcl2["mediator_chain"] == ["TP53", "MDM2"]
        assert bcl2["upstream_interactor"] == "MDM2"
        assert bcl2["depth"] == 3

    def test_reconciles_from_full_chain_query_in_middle(self):
        from runner import _reconcile_chain_fields
        payload = {"ctx_json": {
            "main": "ATXN3",
            "interactors": [
                {"primary": "TARGET", "interaction_type": "indirect",
                 "chain_context": {
                     "full_chain": ["VCP", "HNRNPA1", "ATXN3", "SQSTM1", "TARGET"],
                     "query_protein": "ATXN3",
                     "query_position": 2,
                     "chain_length": 5,
                 }},
            ]
        }}
        result = _reconcile_chain_fields(payload)
        target = result["ctx_json"]["interactors"][0]
        # mediator_chain = full_chain MINUS endpoints, regardless of query pos.
        assert target["mediator_chain"] == ["HNRNPA1", "ATXN3", "SQSTM1"]
        assert target["upstream_interactor"] == "SQSTM1"
        assert target["depth"] == 4

    def test_skips_direct_interactors(self):
        from runner import _reconcile_chain_fields
        payload = {"ctx_json": {
            "main": "ATXN3",
            "interactors": [
                {"primary": "VCP", "interaction_type": "direct", "mediator_chain": []},
            ]
        }}
        result = _reconcile_chain_fields(payload)
        assert result["ctx_json"]["interactors"][0]["mediator_chain"] == []

    def test_missing_full_chain_skips_silently(self, capsys):
        """Reconciler is silent when chain_context.full_chain is absent.

        Pre-step2ab calls fire the reconciler on indirects that don't yet
        have full_chain — that's normal and expected. The [CHAIN SKIP]
        warning is emitted only by the downstream consumer
        (_get_chained_needing_link_functions) at the point where it
        matters (chain-link generation about to skip an interactor).
        """
        from runner import _reconcile_chain_fields
        payload = {"ctx_json": {
            "main": "ATXN3",
            "interactors": [
                {"primary": "BCL2", "interaction_type": "indirect",
                 "upstream_interactor": "TP53", "mediator_chain": []},
            ]
        }}
        result = _reconcile_chain_fields(payload)
        bcl2 = result["ctx_json"]["interactors"][0]
        assert bcl2["mediator_chain"] == []
        err = capsys.readouterr().err
        assert "[CHAIN BUG]" not in err
        assert "[CHAIN]" not in err  # no "Reconciled N" either, since 0 reconciled

    def test_reconciliation_is_idempotent(self):
        from runner import _reconcile_chain_fields
        payload = {"ctx_json": {
            "main": "ATXN3",
            "interactors": [
                {"primary": "BCL2", "interaction_type": "indirect",
                 "chain_context": {
                     "full_chain": ["ATXN3", "TP53", "BCL2"],
                     "query_protein": "ATXN3",
                     "query_position": 0,
                     "chain_length": 3,
                 }},
            ]
        }}
        first = _reconcile_chain_fields(payload)
        second = _reconcile_chain_fields(first)
        assert second["ctx_json"]["interactors"][0]["mediator_chain"] == ["TP53"]
        assert second["ctx_json"]["interactors"][0]["depth"] == 2


# ═══════════════════════════════════════════════════════════════
# _dedup_functions_locally
# ═══════════════════════════════════════════════════════════════


class TestDedupFunctionsLocally:
    # Each test function needs a substantive ``cellular_process`` so that
    # ``_strip_empty_functions`` (pass 0 of the dedup pipeline) doesn't
    # discard the fixtures as content-free before dedup has a chance to run.
    _MECHANISM_TEXT = (
        "VCP extracts misfolded substrates from the ER membrane and delivers "
        "them to the proteasome for ubiquitin-dependent degradation."
    )

    def test_removes_exact_duplicates(self):
        from runner import _dedup_functions_locally
        mech = self._MECHANISM_TEXT
        payload = {"ctx_json": {
            "interactors": [
                {"primary": "VCP", "functions": [
                    {"function": "Apoptosis", "arrow": "activates", "cellular_process": mech},
                    {"function": "Apoptosis", "arrow": "activates", "cellular_process": mech},  # duplicate
                    {"function": "Autophagy", "arrow": "inhibits", "cellular_process": mech},
                ]},
            ]
        }}
        result = _dedup_functions_locally(payload)
        funcs = result["ctx_json"]["interactors"][0]["functions"]
        assert len(funcs) == 2

    def test_case_insensitive_dedup(self):
        from runner import _dedup_functions_locally
        mech = self._MECHANISM_TEXT
        payload = {"ctx_json": {
            "interactors": [
                {"primary": "VCP", "functions": [
                    {"function": "Apoptosis", "arrow": "activates", "cellular_process": mech},
                    {"function": "apoptosis", "arrow": "Activates", "cellular_process": mech},  # same after lower
                ]},
            ]
        }}
        result = _dedup_functions_locally(payload)
        assert len(result["ctx_json"]["interactors"][0]["functions"]) == 1

    def test_no_removal_when_unique(self):
        from runner import _dedup_functions_locally
        mech = self._MECHANISM_TEXT
        payload = {"ctx_json": {
            "interactors": [
                {"primary": "VCP", "functions": [
                    {"function": "Apoptosis", "arrow": "activates", "cellular_process": mech},
                    {"function": "Autophagy", "arrow": "inhibits", "cellular_process": mech},
                ]},
            ]
        }}
        result = _dedup_functions_locally(payload)
        assert len(result["ctx_json"]["interactors"][0]["functions"]) == 2


# ═══════════════════════════════════════════════════════════════
# _get_chained_needing_link_functions (updated)
# ═══════════════════════════════════════════════════════════════


class TestGetChainedNeedingLinkFunctions:
    def test_does_not_auto_fill_chain_from_upstream(self):
        """The ``chain = [upstream]`` fill-in is deliberately gone.

        Indirect interactors without chain_context.full_chain (or a
        usable ``mediator_chain``) get skipped here — chain-link
        generation only fires with explicit chain data, and
        ``mediator_chain`` is NOT mutated in-place by this reader.
        The absence of chain data triggers a single [CHAIN SKIP] log
        summarising every skipped indirect (see separate test).
        """
        from runner import _get_chained_needing_link_functions
        ctx = {
            "interactors": [
                {"primary": "BCL2", "interaction_type": "indirect",
                 "upstream_interactor": "TP53", "mediator_chain": []},
            ]
        }
        result = _get_chained_needing_link_functions(ctx)
        # NEW expectation: BCL2 is NOT returned (skipped because no chain),
        # and mediator_chain is NOT auto-filled.
        assert "BCL2" not in result
        assert ctx["interactors"][0]["mediator_chain"] == []

    def test_silent_when_indirects_lack_chain_data(self, capsys):
        """The diagnostic is silent. A real, consequential missing-claim
        surfaces downstream in utils/db_sync.py as [CHAIN HOP CLAIM MISSING]
        — which fires exactly when a chain hop reaches the DB without a
        claim. Logging here would false-fire at every pre-chain-resolution
        diagnostic call (since step2ab hasn't yet populated full_chain).
        """
        from runner import _get_chained_needing_link_functions
        ctx = {
            "main": "PERK",
            "interactors": [
                {"primary": "ATF4", "interaction_type": "indirect"},
                {"primary": "DDIT3", "interaction_type": "indirect"},
                {"primary": "BBC3", "interaction_type": "indirect"},
                {"primary": "VCP", "interaction_type": "direct"},
            ],
        }
        _get_chained_needing_link_functions(ctx)
        err = capsys.readouterr().err
        assert "[CHAIN SKIP]" not in err
        assert "[CHAIN BUG]" not in err

    def test_skips_direct_interactors(self):
        from runner import _get_chained_needing_link_functions
        ctx = {
            "interactors": [
                {"primary": "VCP", "interaction_type": "direct"},
            ]
        }
        assert _get_chained_needing_link_functions(ctx) == []

    def test_skips_truly_chainless(self):
        from runner import _get_chained_needing_link_functions
        ctx = {
            "interactors": [
                {"primary": "BCL2", "interaction_type": "indirect",
                 "upstream_interactor": None, "mediator_chain": []},
            ]
        }
        assert _get_chained_needing_link_functions(ctx) == []

    def test_includes_citation_verification_step(self):
        from pipeline.config_dynamic import generate_iterative_pipeline
        steps = generate_iterative_pipeline()
        names = [s.name for s in steps]
        assert "step2e_citation_verification" in names


# ═══════════════════════════════════════════════════════════════
# _merge_iteration_output
# ═══════════════════════════════════════════════════════════════


class TestMergeIterationOutput:
    def _import_merge(self):
        from runner import _merge_iteration_output
        return _merge_iteration_output

    def test_first_iteration_initializes(self):
        merge = self._import_merge()
        raw = json.dumps({
            "ctx_json": {
                "main": "ATXN3",
                "interactors": [{"primary": "VCP", "interaction_type": "direct"}],
                "interactor_history": ["VCP"],
            }
        })
        result = merge(None, raw)
        assert result["main"] == "ATXN3"
        assert len(result["interactors"]) == 1

    def test_dedup_by_primary(self):
        merge = self._import_merge()
        existing = {
            "main": "ATXN3",
            "interactors": [{"primary": "VCP", "interaction_type": "direct"}],
            "interactor_history": ["VCP"],
        }
        new_raw = json.dumps({
            "ctx_json": {
                "interactors": [
                    {"primary": "VCP", "interaction_type": "direct"},  # duplicate
                    {"primary": "LAMP2", "interaction_type": "indirect"},  # new
                ],
                "interactor_history": ["VCP", "LAMP2"],
            }
        })
        result = merge(existing, new_raw)
        names = [i["primary"] for i in result["interactors"]]
        assert names == ["VCP", "LAMP2"]

    def test_merges_tracking_arrays(self):
        merge = self._import_merge()
        existing = {
            "main": "ATXN3",
            "interactors": [],
            "search_history": ["query1"],
        }
        new_raw = json.dumps({
            "ctx_json": {
                "interactors": [],
                "search_history": ["query1", "query2"],
            }
        })
        result = merge(existing, new_raw)
        assert "query2" in result["search_history"]
        # No duplicates
        assert result["search_history"].count("query1") == 1

    def test_enriches_existing_interactor(self):
        merge = self._import_merge()
        existing = {
            "main": "ATXN3",
            "interactors": [{"primary": "VCP", "interaction_type": "direct"}],
            "interactor_history": ["VCP"],
        }
        new_raw = json.dumps({
            "ctx_json": {
                "interactors": [
                    {
                        "primary": "VCP",
                        "interaction_type": "direct",
                        "support_summary": "Co-IP confirmed binding",
                    },
                ],
                "interactor_history": ["VCP"],
            }
        })
        result = merge(existing, new_raw)
        vcp = result["interactors"][0]
        assert vcp["support_summary"] == "Co-IP confirmed binding"

    def test_handles_malformed_json(self):
        merge = self._import_merge()
        result = merge(None, "not json at all")
        assert result == {}

    def test_handles_json_without_ctx_json(self):
        merge = self._import_merge()
        raw = json.dumps({"something": "else"})
        result = merge(None, raw)
        assert isinstance(result, dict)


# ═══════════════════════════════════════════════════════════════
# _build_iteration_prompt
# ═══════════════════════════════════════════════════════════════


class TestBuildIterationPrompt:
    def _import_build(self):
        from runner import _build_iteration_prompt
        return _build_iteration_prompt

    def test_first_iteration_uses_base_prompt(self):
        build = self._import_build()
        cfg = IterationConfig(name="broad", focus="Broad discovery", prompt_template="ITER PROMPT")
        result = build(cfg, "BASE PROMPT", None, 0, 5)
        assert "ITERATION 1 of 5" in result
        assert "BASE PROMPT" in result

    def test_subsequent_iteration_includes_context(self):
        build = self._import_build()
        cfg = IterationConfig(name="pathway", focus="Pathway partners", prompt_template="PATHWAY PROMPT")
        accumulated = {
            "interactors": [{"primary": "VCP"}],
            "interactor_history": ["VCP"],
        }
        result = build(cfg, "", accumulated, 1, 5)
        assert "ITERATION 2 of 5" in result
        assert "VCP" in result
        assert "PATHWAY PROMPT" in result

    def test_search_hints_included(self):
        build = self._import_build()
        cfg = IterationConfig(
            name="broad",
            focus="Broad",
            prompt_template="test",
            search_queries_hint=("ATXN3 interactions",),
        )
        result = build(cfg, "", None, 0, 1)
        assert "ATXN3 interactions" in result


# ═══════════════════════════════════════════════════════════════
# API mode dispatch
# ═══════════════════════════════════════════════════════════════


class TestDispatchRoutesToHandler:
    @patch("runner._call_iterative_research_mode")
    def test_iterative_research_dispatches(self, mock_handler):
        from runner import call_gemini_model
        mock_handler.return_value = ('{"ctx_json": {}}', {"prompt_tokens": 0})

        step = StepConfig(
            name="step1_iterative_research_discovery",
            model="gemini-3.1-pro-preview",
            api_mode="iterative_research",
            prompt_template="test",
            expected_columns=["ctx_json"],
            iteration_configs=(
                IterationConfig(name="test", focus="test", prompt_template="test"),
            ),
        )

        with patch.dict("os.environ", {"GOOGLE_CLOUD_PROJECT": "test-project", "GOOGLE_CLOUD_LOCATION": "us-central1"}):
            call_gemini_model(step, "test prompt")

        mock_handler.assert_called_once()


# ═══════════════════════════════════════════════════════════════
# Model registry
# ═══════════════════════════════════════════════════════════════


class TestModelRegistry:
    def test_iterative_model_in_registry(self):
        from utils.gemini_runtime import MODEL_REGISTRY
        assert "iterative" in MODEL_REGISTRY
        assert "gemini-3-flash-preview" in MODEL_REGISTRY["iterative"]

    def test_get_model_iterative(self):
        from utils.gemini_runtime import get_model
        model = get_model("iterative")
        assert "gemini" in model.lower()
