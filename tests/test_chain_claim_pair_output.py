import json


def test_pair_keyed_chain_claim_output_attaches_to_target_slot():
    from runner import _merge_chain_claim_output, _missing_chain_claim_pairs

    payload = {
        "ctx_json": {
            "main": "ATXN3",
            "_chain_pair_context": {
                "BECN1->PIK3C3": {
                    "full_chain": ["ATXN3", "BECN1", "PIK3C3"],
                    "hop_index": 1,
                }
            },
            "interactors": [
                {"primary": "BECN1", "functions": []},
                {"primary": "PIK3C3", "functions": []},
            ],
        }
    }
    raw = json.dumps({
        "chain_claims": [
            {
                "pair": "BECN1->PIK3C3",
                "source": "BECN1",
                "target": "PIK3C3",
                "functions": [
                    {
                        "function": "PIK3C3 recruitment by BECN1",
                        "arrow": "activates",
                        "cellular_process": "BECN1 recruits PIK3C3.",
                        "effect_description": "PIK3C3 activity increases.",
                    }
                ],
            }
        ]
    })

    merged = _merge_chain_claim_output(
        raw,
        payload,
        ["BECN1->PIK3C3"],
        "unit_chain_claims",
    )

    target = next(
        i for i in merged["ctx_json"]["interactors"]
        if i["primary"] == "PIK3C3"
    )
    funcs = target["chain_link_functions"]["BECN1->PIK3C3"]
    assert funcs[0]["function"] == "PIK3C3 recruitment by BECN1"
    assert funcs[0]["function_context"] == "chain_derived"
    assert _missing_chain_claim_pairs(
        merged["ctx_json"], ["BECN1->PIK3C3"]
    ) == []


def test_chain_claim_output_mirrors_to_chain_owner_slots():
    from runner import _merge_chain_claim_output

    payload = {
        "ctx_json": {
            "main": "ATXN3",
            "_chain_pair_context": {
                "BECN1->PIK3C3": {
                    "full_chain": ["ATXN3", "BECN1", "PIK3C3", "ATG14"],
                    "hop_index": 1,
                }
            },
            "interactors": [
                {"primary": "PIK3C3", "functions": []},
                {
                    "primary": "ATG14",
                    "interaction_type": "indirect",
                    "functions": [],
                    "chain_context": {
                        "full_chain": ["ATXN3", "BECN1", "PIK3C3", "ATG14"],
                        "query_protein": "ATXN3",
                        "query_position": 0,
                    },
                },
            ],
        }
    }
    raw = json.dumps({
        "chain_claims": [
            {
                "pair": "BECN1->PIK3C3",
                "source": "BECN1",
                "target": "PIK3C3",
                "functions": [{"function": "BECN1 scaffolds PIK3C3"}],
            }
        ]
    })

    merged = _merge_chain_claim_output(
        raw,
        payload,
        ["BECN1->PIK3C3"],
        "unit_chain_claims",
    )

    by_primary = {
        item["primary"]: item for item in merged["ctx_json"]["interactors"]
    }
    assert "BECN1->PIK3C3" in by_primary["PIK3C3"]["chain_link_functions"]
    assert "BECN1->PIK3C3" in by_primary["ATG14"]["chain_link_functions"]


def test_chain_claim_targets_include_query_tail_hops(monkeypatch):
    monkeypatch.setenv("CHAIN_CLAIM_DB_REHYDRATE", "false")

    from runner import _get_chain_claim_targets

    ctx = {
        "main": "ATXN3",
        "interactors": [
            {
                "primary": "PINK1",
                "interaction_type": "indirect",
                "functions": [],
                "chain_context": {
                    "full_chain": ["PINK1", "PARK2", "ATXN3"],
                    "query_protein": "ATXN3",
                    "query_position": 2,
                },
            }
        ],
    }

    targets = _get_chain_claim_targets(ctx, "step2ax_claim_generation_explicit")

    assert "PINK1->PARK2" in targets
    assert "PARK2->ATXN3" in targets


def test_chain_claim_step_uses_pair_keyed_schema(monkeypatch):
    monkeypatch.setenv("CHAIN_CLAIM_MAX_OUTPUT_TOKENS", "8192")
    monkeypatch.setenv("CHAIN_CLAIM_TEMPERATURE", "0.2")

    from pipeline.prompts.deep_research_steps import (
        step2ax_claim_generation_explicit,
    )

    step = step2ax_claim_generation_explicit()

    assert list(step.expected_columns) == ["chain_claims"]
    assert step.response_schema["required"] == ["chain_claims"]
    claim_item = step.response_schema["properties"]["chain_claims"]["items"]
    fn_item = claim_item["properties"]["functions"]["items"]
    assert claim_item["required"] == ["pair", "source", "target", "functions"]
    assert "function_context" in fn_item["properties"]
    assert "biological_consequence" in fn_item["required"]
    assert step.max_output_tokens == 8192
    assert step.temperature == 0.2


def test_web_chain_claim_dispatch_uses_chain_specific_throttle():
    import inspect
    import runner

    source = (
        inspect.getsource(runner.run_pipeline)
        + "\n"
        + inspect.getsource(runner._run_main_pipeline_for_web)
    )

    assert source.count('_chain_kwargs["request_mode"] = CHAIN_CLAIM_REQUEST_MODE') >= 2
    assert source.count("max_workers=CHAIN_CLAIM_MAX_WORKERS") >= 2
    assert source.count("rate_limit_group_size=CHAIN_CLAIM_MAX_WORKERS") >= 2
    assert source.count("retry_max_workers=CHAIN_CLAIM_RECOVERY_MAX_WORKERS") >= 2
    assert (
        "batch_size=CHAIN_CLAIM_BATCH_SIZE,\n"
        "                        max_workers=PARALLEL_MAX_WORKERS"
    ) not in source


def test_parallel_dispatch_uses_aliased_future_wait():
    import inspect
    import runner

    source = inspect.getsource(runner._run_parallel_batched_phase)

    assert "futures_wait(" in source
    assert "done, pending = wait(" not in source


def test_chain_claim_dispatch_is_rolling_not_group_barrier():
    import inspect
    import runner

    source = inspect.getsource(runner._run_parallel_batched_phase)

    assert "CHAIN_CLAIM_ROLLING_DISPATCH" in source
    assert "CHAIN_CLAIM_ADAPTIVE_DISPATCH" in source
    assert "Adaptive throttle" in source
    assert "Finished slots immediately launch the next batch" in source
    assert "while len(active) < target_workers and _submit_next_call(executor)" in source


def test_function_mapping_uses_rolling_and_compact_retry():
    import inspect
    import runner

    source = inspect.getsource(runner._run_parallel_batched_phase)
    call_source = inspect.getsource(runner.call_gemini_model)

    assert "FUNCTION_MAPPING_ROLLING_DISPATCH" in source
    assert "FAILED-CALL COMPACT RETRY" in source
    assert "FUNCTION_MAPPING_FAILED_BATCH_RETRIES" in source
    assert "FUNCTION_MAPPING_REQUEST_TIMEOUT_MS" in call_source
    assert "FUNCTION_MAPPING_MAX_RETRIES" in call_source
