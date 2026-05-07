"""Tests for the modular prompt architecture.

Covers: StepConfig extension, shared_blocks constants, step factories,
prompt composition, pipeline equivalence, and backward compatibility.
"""
from __future__ import annotations

import pytest

from pipeline.types import StepConfig


# ----------------------------------------------------------------
# A. StepConfig extension tests
# ----------------------------------------------------------------


class TestStepConfigExtension:
    """Verify new fields have defaults and don't break existing usage."""

    def test_existing_fields_still_work(self):
        step = StepConfig(
            name="test",
            model="gemini-3.1-pro-preview",
            prompt_template="Do something.",
            expected_columns=["ctx_json"],
        )
        assert step.name == "test"
        assert step.api_mode == "generate"
        assert step.cache_system_prompt is False
        assert step.depends_on is None
        assert step.parallel_group is None
        assert step.retry_strategy == "exponential"

    def test_new_fields_explicitly_set(self):
        step = StepConfig(
            name="test",
            model="m",
            prompt_template="p",
            expected_columns=["x"],
            api_mode="batch",
            cache_system_prompt=False,
            depends_on="step1a_discover",
            parallel_group="discovery",
            retry_strategy="linear",
        )
        assert step.api_mode == "batch"
        assert step.cache_system_prompt is False
        assert step.depends_on == "step1a_discover"
        assert step.parallel_group == "discovery"
        assert step.retry_strategy == "linear"

    def test_frozen_immutability(self):
        step = StepConfig(
            name="test", model="m", prompt_template="p", expected_columns=["x"]
        )
        with pytest.raises(AttributeError):
            step.api_mode = "batch"  # type: ignore[misc]

    def test_validation_still_fires_empty_name(self):
        with pytest.raises(ValueError, match="name cannot be empty"):
            StepConfig(name="", model="m", prompt_template="p", expected_columns=["x"])

    def test_validation_still_fires_empty_model(self):
        with pytest.raises(ValueError, match="must specify a model"):
            StepConfig(
                name="test", model="", prompt_template="p", expected_columns=["x"]
            )

    def test_validation_still_fires_empty_prompt(self):
        with pytest.raises(ValueError, match="must include a prompt_template"):
            StepConfig(
                name="test", model="m", prompt_template="  ", expected_columns=["x"]
            )

    def test_validation_still_fires_empty_columns(self):
        with pytest.raises(ValueError, match="must define expected_columns"):
            StepConfig(name="test", model="m", prompt_template="p", expected_columns=[])


# ----------------------------------------------------------------
# B. Shared blocks tests
# ----------------------------------------------------------------


class TestSharedBlocks:
    """Verify text constants are intact and helpers work."""

    def test_all_constants_non_empty(self):
        from pipeline.prompts.shared_blocks import (
            CONTENT_DEPTH_REQUIREMENTS,
            DIFFERENTIAL_OUTPUT_RULES,
            FUNCTION_NAMING_RULES,
            INTERACTOR_TYPES,
            SCHEMA_HELP,
            STRICT_GUARDRAILS,
        )

        for name, const in [
            ("DIFFERENTIAL_OUTPUT_RULES", DIFFERENTIAL_OUTPUT_RULES),
            ("STRICT_GUARDRAILS", STRICT_GUARDRAILS),
            ("SCHEMA_HELP", SCHEMA_HELP),
            ("FUNCTION_NAMING_RULES", FUNCTION_NAMING_RULES),
            ("CONTENT_DEPTH_REQUIREMENTS", CONTENT_DEPTH_REQUIREMENTS),
            ("INTERACTOR_TYPES", INTERACTOR_TYPES),
        ]:
            assert len(const) > 100, f"{name} seems too short: {len(const)} chars"

    def test_constants_contain_expected_markers(self):
        from pipeline.prompts.shared_blocks import (
            CONTENT_DEPTH_REQUIREMENTS,
            DIFFERENTIAL_OUTPUT_RULES,
            FUNCTION_NAMING_RULES,
            INTERACTOR_TYPES,
            SCHEMA_HELP,
            STRICT_GUARDRAILS,
        )

        assert "DIFFERENTIAL OUTPUT RULES" in DIFFERENTIAL_OUTPUT_RULES
        assert "GROUNDING RULES" in STRICT_GUARDRAILS
        assert "SCHEMA SPECIFICATION" in SCHEMA_HELP
        assert "FUNCTION NAMING" in FUNCTION_NAMING_RULES
        assert "CONTENT DEPTH REQUIREMENTS" in CONTENT_DEPTH_REQUIREMENTS
        assert "INTERACTOR CLASSIFICATION" in INTERACTOR_TYPES

    def test_module_constants(self):
        from pipeline.prompts.shared_blocks import (
            DYNAMIC_SEARCH_THRESHOLD,
            MAX_OUTPUT_TOKENS,
        )

        assert MAX_OUTPUT_TOKENS == 60000
        assert DYNAMIC_SEARCH_THRESHOLD == 0.0

    def test_get_system_prompt_text_returns_string(self):
        from pipeline.prompts.shared_blocks import get_system_prompt_text

        text = get_system_prompt_text()
        assert isinstance(text, str)
        assert len(text) > 200
        assert "GROUNDING RULES" in text.upper()


# ----------------------------------------------------------------
# C. Step factory tests
# ----------------------------------------------------------------


class TestStepFactories:
    """Verify each factory returns a valid StepConfig."""

    def test_discovery_factories_produce_valid_steps(self):
        from pipeline.prompts.interactor_discovery import BASE_DISCOVERY_FACTORIES

        names = set()
        for factory in BASE_DISCOVERY_FACTORIES:
            step = factory()
            assert isinstance(step, StepConfig)
            assert step.name.startswith("step1")
            assert list(step.expected_columns) == ["ctx_json", "step_json"]
            assert step.name not in names, f"Duplicate step name: {step.name}"
            names.add(step.name)
        assert len(names) == 7

    def test_function_factories_produce_valid_steps(self):
        from pipeline.prompts.function_mapping import BASE_FUNCTION_FACTORIES

        for factory in BASE_FUNCTION_FACTORIES:
            step = factory()
            assert isinstance(step, StepConfig)
            assert "step2a" in step.name
            assert list(step.expected_columns) == ["ctx_json", "step_json"]

    def test_chain_resolution_factories(self):
        from pipeline.prompts.deep_research_steps import (
            step2ab_chain_determination,
            step2ab2_hidden_indirect_detection,
            step2ab3_hidden_chain_determination,
            step2ab5_extract_pairs_explicit,
            step2ax_claim_generation_explicit,
            step2az_claim_generation_hidden,
        )

        for factory in [
            step2ab_chain_determination,
            step2ab2_hidden_indirect_detection,
            step2ab3_hidden_chain_determination,
            step2ab5_extract_pairs_explicit,
            step2ax_claim_generation_explicit,
            step2az_claim_generation_hidden,
        ]:
            step = factory()
            assert isinstance(step, StepConfig)
            assert step.name.startswith("step2a")

    def test_arrow_template_factory(self):
        from pipeline.prompts.arrow_determination import step2c_arrow_template

        step = step2c_arrow_template()
        assert step.name == "step2c_arrow_TEMPLATE"
        assert "{INTERACTOR}" in step.prompt_template

    def test_qc_and_snapshot_factories(self):
        from pipeline.prompts.qc_and_snapshot import step2g_final_qc, step3_snapshot

        qc = step2g_final_qc()
        assert qc.name == "step2g_final_qc"
        assert qc.use_google_search is False  # QC uses Flash without search

        snap = step3_snapshot()
        assert snap.name == "step3_snapshot"
        assert snap.use_google_search is False
        assert list(snap.expected_columns) == [
            "ctx_json",
            "snapshot_json",
            "ndjson",
            "step_json",
        ]

    def test_dynamic_interactor_step(self):
        from pipeline.prompts.interactor_discovery import (
            create_interactor_discovery_step,
        )

        step = create_interactor_discovery_step(8)
        assert step.name == "step1j_discover_round8"
        assert "Round 8" in step.prompt_template or "EIGHTH" in step.prompt_template

    def test_dynamic_function_step(self):
        from pipeline.prompts.function_mapping import create_function_mapping_step

        step = create_function_mapping_step(6)
        assert "round6" in step.name

    def test_all_factories_include_preamble_blocks(self):
        from pipeline.prompts.interactor_discovery import BASE_DISCOVERY_FACTORIES
        from pipeline.prompts.shared_blocks import (
            DIFFERENTIAL_OUTPUT_RULES,
            INTERACTOR_TYPES,
        )

        for factory in BASE_DISCOVERY_FACTORIES:
            step = factory()
            # Preamble may be in prompt_template or system_prompt (if cached)
            combined = (step.prompt_template or "") + (step.system_prompt or "")
            assert DIFFERENTIAL_OUTPUT_RULES in combined
            assert INTERACTOR_TYPES in combined


# ----------------------------------------------------------------
# D. Pipeline equivalence tests
# ----------------------------------------------------------------


class TestPipelineEquivalence:
    """Verify generate_pipeline output is correct."""

    def test_default_pipeline_step_names(self):
        from pipeline.config_dynamic import generate_pipeline

        steps = generate_pipeline(3, 3)
        names = [s.name for s in steps]
        assert names[0] == "step1a_discover"
        assert names[1] == "step1b_expand"
        assert names[2] == "step1c_deep_mining"
        assert "step2a_functions" in names
        assert names[-1] == "step3_snapshot"
        assert "step2c_arrow_TEMPLATE" not in names

    def test_base_pipeline_includes_arrow_template(self):
        from pipeline.prompts import build_base_pipeline_steps

        PIPELINE_STEPS = build_base_pipeline_steps()
        names = [s.name for s in PIPELINE_STEPS]
        assert "step2c_arrow_TEMPLATE" in names

    def test_generate_pipeline_step_counts(self):
        from pipeline.config_dynamic import generate_pipeline

        # 1 discovery + 1 function + 3 visible chain stages + 1 snapshot.
        assert len(generate_pipeline(1, 1)) == 6
        # 7 discovery + 5 function + 3 visible chain stages + 1 snapshot.
        assert len(generate_pipeline(7, 5)) == 16
        # 10 discovery + 8 function + 3 visible chain stages + 1 snapshot.
        assert len(generate_pipeline(10, 8)) == 22

    def test_generate_pipeline_clamps_inputs(self):
        from pipeline.config_dynamic import generate_pipeline

        # min 1
        steps_0 = generate_pipeline(0, 0)
        assert len(steps_0) == 6  # 1+1+3+1
        # max 10
        steps_99 = generate_pipeline(99, 99)
        assert len(steps_99) == 24  # 10+10+3+1

    def test_all_steps_pass_validation(self):
        from pipeline.config_dynamic import generate_pipeline

        steps = generate_pipeline(7, 5)
        for step in steps:
            assert step.name
            assert step.model
            assert step.prompt_template.strip()
            assert list(step.expected_columns)


# ----------------------------------------------------------------
# E. Backward compatibility tests
# ----------------------------------------------------------------


class TestBackwardCompatibility:
    """Verify all existing import paths still work."""

    def test_import_constants_from_config_dynamic(self):
        from pipeline.config_dynamic import (
            CONTENT_DEPTH_REQUIREMENTS,
            DIFFERENTIAL_OUTPUT_RULES,
            FUNCTION_NAMING_RULES,
            INTERACTOR_TYPES,
            STRICT_GUARDRAILS,
        )

        assert len(DIFFERENTIAL_OUTPUT_RULES) > 100
        assert len(STRICT_GUARDRAILS) > 100
        assert len(FUNCTION_NAMING_RULES) > 100
        assert len(CONTENT_DEPTH_REQUIREMENTS) > 100
        assert len(INTERACTOR_TYPES) > 100

    def test_import_generate_pipeline_from_config_dynamic(self):
        from pipeline.config_dynamic import PIPELINE_STEPS, generate_pipeline

        assert callable(generate_pipeline)
        assert len(PIPELINE_STEPS) > 0

    def test_runner_import_pattern_works(self):
        """Simulate runner.py's import pattern."""
        from pipeline.config_dynamic import (
            PIPELINE_STEPS as DEFAULT_PIPELINE_STEPS,
        )
        from pipeline.config_dynamic import generate_pipeline

        assert callable(generate_pipeline)
        assert len(DEFAULT_PIPELINE_STEPS) > 0

    def test_constants_identical_across_import_paths(self):
        """Constants from prompts and config_dynamic must be identical."""
        from pipeline.config_dynamic import (
            DIFFERENTIAL_OUTPUT_RULES as cd_rules,
        )
        from pipeline.prompts.shared_blocks import (
            DIFFERENTIAL_OUTPUT_RULES as sb_rules,
        )

        assert sb_rules is cd_rules
