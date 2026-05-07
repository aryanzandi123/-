"""Shared data structures for configuring the Gemini pipeline."""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Optional, Sequence


@dataclass(frozen=True)
class IterationConfig:
    """Configuration for a single research iteration within iterative_research mode.

    Attributes
    ----------
    name:
        Human-readable label (e.g. 'broad_discovery').
    focus:
        One-line description of the iteration's research focus.
    prompt_template:
        Prompt text with {user_query} placeholder.
    search_queries_hint:
        Suggested search patterns the model should execute.
    """

    name: str
    focus: str
    prompt_template: str
    search_queries_hint: Sequence[str] = ()

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("IterationConfig.name cannot be empty")
        if not self.prompt_template.strip():
            raise ValueError(
                f"IterationConfig '{self.name}' must include a prompt_template"
            )


@dataclass(frozen=True)
class StepConfig:
    """Configuration for a single pipeline step.

    Attributes
    ----------
    name:
        Human-friendly identifier for the step. Must be unique across the pipeline.
    model:
        Gemini model ID to use for this step.
    prompt_template:
        Primary instructions given to the model. The runner will append CSV handling
        guidelines and the prior dataset automatically.
    expected_columns:
        Ordered sequence of column names that this step must output in the CSV.
    system_prompt:
        Optional override for the system prompt. When provided, deep_research is ignored.
    use_google_search:
        Whether to enable the Google Search grounding tool for this step.
    thinking_level:
        Optional Gemini 3 thinking level (e.g., "low", "high").
    max_output_tokens:
        Upper bound on response tokens emitted by the model.
    temperature:
        Optional sampling temperature. When omitted, Gemini SDK/model defaults apply.
    search_dynamic_mode:
        When True, enables dynamic Google Search retrieval mode for aggressive research.
    search_dynamic_threshold:
        Optional stopping threshold for dynamic retrieval (lower values favor more searches).
    api_mode:
        Transport mode for this step ('generate', 'interaction', 'deep_research',
        'iterative_research').
    cache_system_prompt:
        When True, the runner may cache the system_prompt via Gemini's explicit caching API.
    depends_on:
        Optional name of a prior step that must complete before this one can start.
    parallel_group:
        Optional group identifier; steps sharing the same group can execute concurrently.
    retry_strategy:
        Retry strategy on transient failures ('exponential', 'linear', 'none').
    """

    name: str
    model: str
    prompt_template: str
    expected_columns: Sequence[str]
    system_prompt: Optional[str] = None
    use_google_search: bool = True
    thinking_level: Optional[str] = "high"
    max_output_tokens: Optional[int] = 65536
    temperature: Optional[float] = None
    search_dynamic_mode: bool = False
    search_dynamic_threshold: Optional[float] = None
    api_mode: str = "generate"
    cache_system_prompt: bool = False
    depends_on: Optional[str] = None
    parallel_group: Optional[str] = None
    retry_strategy: str = "exponential"
    response_schema: Optional[Dict[str, Any]] = None
    iteration_configs: Optional[tuple] = None  # tuple[IterationConfig, ...] for iterative_research

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("StepConfig.name cannot be empty")
        if not self.model:
            raise ValueError(f"Step '{self.name}' must specify a model")
        if not self.prompt_template.strip():
            raise ValueError(f"Step '{self.name}' must include a prompt_template")
        if not list(self.expected_columns):
            raise ValueError(f"Step '{self.name}' must define expected_columns")
        if self.api_mode == "iterative_research" and not self.iteration_configs:
            raise ValueError(
                f"Step '{self.name}' with api_mode='iterative_research' "
                "requires non-empty iteration_configs"
            )


def as_columns(columns: Iterable[str]) -> list[str]:
    """Return a list of stripped column names for convenience."""

    return [col.strip() for col in columns]


# ---------------------------------------------------------------------------
# Structured output schemas for Gemini response_json_schema
# ---------------------------------------------------------------------------

# Pristine base interactor schema — never mutated. Any module that wants an
# extensible copy should `copy.deepcopy(_INTERACTOR_OBJECT_BASE)` to start fresh.
_INTERACTOR_OBJECT_BASE = {
    "type": "object",
    "properties": {
        "primary": {"type": "string"},
        "interaction_type": {"type": "string", "enum": ["direct", "indirect"]},
        "upstream_interactor": {"type": ["string", "null"]},
        "mediator_chain": {"type": "array", "items": {"type": "string"}},
        "depth": {"type": "integer"},
        "support_summary": {"type": "string"},
        # Query-position-agnostic chain metadata. When present, ``full_chain``
        # is the authoritative biological ordering with the query protein at
        # whatever position the biology places it — not forced to index 0.
        # This is what makes chains with the query in the middle (``A → B →
        # QUERY → C → D``) representable. db_sync's ``_write_indirect_chain_links``
        # already honors it case-insensitively, and the frontend
        # ``buildFullChainPath`` reads it before any legacy reconstruction.
        "chain_context": {
            "type": "object",
            "properties": {
                "full_chain": {"type": "array", "items": {"type": "string"}},
            },
        },
    },
    "required": ["primary", "interaction_type"],
}

# Discovery uses a pristine deepcopy — no functions or chain_link_functions.
_DISCOVERY_INTERACTOR = copy.deepcopy(_INTERACTOR_OBJECT_BASE)

# Function-mapping uses an independent deepcopy that is extended below
# with chain_link_functions and _chain_pathway properties.
_INTERACTOR_OBJECT = copy.deepcopy(_INTERACTOR_OBJECT_BASE)

DISCOVERY_OUTPUT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "ctx_json": {
            "type": "object",
            "properties": {
                "main": {"type": "string"},
                "interactors": {"type": "array", "items": _DISCOVERY_INTERACTOR},
                "interactor_history": {"type": "array", "items": {"type": "string"}},
                "search_history": {"type": "array", "items": {"type": "string"}},
                # Proteins that act on the query (upstream regulators). Populated
                # by the dedicated "upstream context" discovery iteration so
                # downstream iterations know which proteins sit upstream of the
                # query and can position them correctly in any chains they
                # emit via ``chain_context.full_chain``.
                "upstream_of_main": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["main", "interactors"],
        },
        "step_json": {
            "type": "object",
            "properties": {
                "step": {"type": "string"},
                "count": {"type": "integer"},
            },
        },
    },
    "required": ["ctx_json"],
}

_EVIDENCE_ENTRY = {
    "type": "object",
    "properties": {
        "paper_title": {"type": "string"},
        "relevant_quote": {"type": "string"},
        "year": {"type": "integer"},
        "assay": {"type": "string"},
        "species": {"type": "string"},
        "key_finding": {"type": "string"},
    },
    "required": ["paper_title", "relevant_quote", "year"],
}

_FUNCTION_OBJECT = {
    "type": "object",
    "properties": {
        "function": {"type": "string"},
        "arrow": {"type": "string", "enum": ["activates", "inhibits", "binds", "regulates"]},
        "interaction_direction": {"type": "string", "enum": ["main_to_primary", "primary_to_main"]},
        "pathway": {"type": "string"},
        "cellular_process": {"type": "string"},
        "effect_description": {"type": "string"},
        "biological_consequence": {"type": "array", "items": {"type": "string"}},
        "specific_effects": {"type": "array", "items": {"type": "string"}},
        "evidence": {"type": "array", "items": _EVIDENCE_ENTRY},
        # P-C1: function_context labels the discovery perspective so the DB
        # claim writer can populate ``InteractionClaim.function_context``
        # (enum-constrained column). Must be one of the four values below;
        # see FUNCTION_CONTEXT_LABELING in shared_blocks.py for when to
        # use each. ``mixed`` is reserved for post-processing, not the LLM.
        "function_context": {
            "type": "string",
            "enum": ["direct", "net", "chain_derived"],
        },
    },
    "required": ["function", "arrow", "cellular_process", "effect_description",
                 "biological_consequence", "specific_effects", "evidence",
                 "function_context"],
}

# Extend interactor schema with chain link function support
_INTERACTOR_OBJECT["properties"]["chain_link_functions"] = {
    "type": "object",
    "additionalProperties": {
        "type": "array",
        "items": _FUNCTION_OBJECT
    }
}
_INTERACTOR_OBJECT["properties"]["_chain_pathway"] = {"type": ["string", "null"]}

# P-H1: chain_with_arrows is a per-hop typed-arrow description the
# LLM emits alongside chain_link_functions for indirect interactors.
# Each entry is a ``{from, to, arrow}`` object covering one mediator
# hop in order. The runner mirrors this into ``Interaction.chain_with_arrows``
# and ``IndirectChain.chain_with_arrows`` columns so the frontend
# can render typed arrows per chain segment without reconstructing
# them from function text.
_INTERACTOR_OBJECT["properties"]["chain_with_arrows"] = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "from": {"type": "string"},
            "to": {"type": "string"},
            "arrow": {
                "type": "string",
                "enum": ["activates", "inhibits", "binds", "regulates"],
            },
        },
        "required": ["from", "to", "arrow"],
    },
}

FUNCTION_MAPPING_OUTPUT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "ctx_json": {
            "type": "object",
            "properties": {
                "main": {"type": "string"},
                "interactors": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "primary": {"type": "string"},
                            "functions": {"type": "array", "items": _FUNCTION_OBJECT},
                            "chain_link_functions": {"type": "object"},
                            "chain_with_arrows": {"type": "array"},
                            "_chain_pathway": {"type": "string"},
                        },
                        "required": ["primary"],
                    },
                },
                "function_batches": {"type": "array", "items": {"type": "string"}},
                "indirect_interactors": {"type": "array"},
            },
            "required": ["main", "interactors"],
        },
        "step_json": {"type": "object"},
    },
    "required": ["ctx_json"],
}

# Evidence-only delta schema for citation verification.
# Includes merge-key fields used by deep_merge_interactors._fn_signature
# (function, cellular_process, interaction_direction) plus evidence and pmids.
# _fn_signature uses interaction_direction (NOT arrow) for direction matching,
# so we must include it for proper merge alignment.
_EVIDENCE_DELTA_FUNCTION = {
    "type": "object",
    "properties": {
        "function": {"type": "string"},
        "cellular_process": {"type": "string"},
        "arrow": {
            "type": "string",
            "enum": ["activates", "inhibits", "binds", "regulates"],
        },
        "interaction_direction": {
            "type": "string",
            "enum": ["main_to_primary", "primary_to_main"],
        },
        "evidence": {"type": "array", "items": _EVIDENCE_ENTRY},
        "pmids": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["function", "cellular_process", "evidence"],
}

CITATION_DELTA_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "ctx_json": {
            "type": "object",
            "properties": {
                "main": {"type": "string"},
                "interactors": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "primary": {"type": "string"},
                            "functions": {
                                "type": "array",
                                "items": _EVIDENCE_DELTA_FUNCTION,
                            },
                        },
                        "required": ["primary", "functions"],
                    },
                },
            },
            "required": ["main", "interactors"],
        },
    },
    "required": ["ctx_json"],
}

QC_OUTPUT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "ctx_json": {
            "type": "object",
            "properties": {
                "main": {"type": "string"},
                "interactors": {"type": "array"},
                "flagged_for_enrichment": {"type": "array", "items": {"type": "string"}},
                "depth_check_passed": {"type": "integer"},
                "depth_check_failed": {"type": "integer"},
            },
            "required": ["main", "interactors"],
        },
        "step_json": {"type": "object"},
    },
    "required": ["ctx_json"],
}

DEDUP_OUTPUT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "duplicate": {"type": "string", "enum": ["YES", "NO"]},
        "better": {"type": "string", "enum": ["1", "2", "EQUAL"]},
        "reason": {"type": "string"},
    },
    "required": ["duplicate", "better", "reason"],
}

DEDUP_BATCH_OUTPUT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "keep_indices": {
            "type": "array",
            "items": {"type": "integer"},
            "description": "Zero-based indices of functions to keep (one per duplicate group, the best representative)",
        },
        "groups": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "indices": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "All indices in this duplicate group",
                    },
                    "kept": {"type": "integer", "description": "Index of the best function in this group"},
                    "reason": {"type": "string"},
                },
                "required": ["indices", "kept", "reason"],
            },
        },
    },
    "required": ["keep_indices"],
}

EVIDENCE_VALIDATION_OUTPUT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "interactors": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "primary": {"type": "string"},
                    "is_valid": {"type": "boolean"},
                    "mechanism_correction": {"type": "string"},
                    "functions": {"type": "array"},
                },
                "required": ["primary", "is_valid"],
            },
        },
    },
    "required": ["interactors"],
}

CITATION_FINDER_OUTPUT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "citations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "function_name": {"type": "string"},
                    "paper_title": {"type": "string"},
                    "pmid": {"type": ["integer", "null"]},
                    "year": {"type": ["integer", "null"]},
                    "journal": {"type": "string"},
                    "relevant_finding": {"type": "string"},
                },
                "required": ["function_name", "paper_title"],
            },
        },
    },
    "required": ["citations"],
}

ARROW_VALIDATION_OUTPUT_SCHEMA: Dict[str, Any] = {
    # Tightened shape: Gemini enforces ``response_json_schema`` at emit
    # time, so removing the ``reasoning`` paragraph per function and the
    # free-text ``validation_summary`` caps per-function output at ~50
    # tokens (arrow labels + enum-ish effect strings) instead of ~400
    # tokens (narrative). For TDP43-scale interactors with 10+ functions
    # this was the root cause of the 16K-token responses that tripped
    # the 8192 ceiling on gemini-3-flash-preview.
    #
    # Corrections are expressed as explicit per-function fields rather
    # than a loose ``corrections: object``, so the model can't emit
    # arbitrary keys back. apply_corrections still accepts the legacy
    # ``{"corrections": {...}}`` nested shape for already-cached
    # responses and unit tests.
    "type": "object",
    "properties": {
        "interaction_level": {
            "type": "object",
            "properties": {
                "direction": {"type": "string"},
                "arrow": {"type": "string"},
            },
        },
        "functions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "function": {"type": "string"},
                    "arrow": {"type": "string"},
                    "direct_arrow": {"type": "string"},
                    "interaction_effect": {"type": "string"},
                    "interaction_direction": {"type": "string"},
                },
                "required": ["function"],
            },
        },
    },
}

# ---------------------------------------------------------------------------
# Chain resolution pipeline schemas
# ---------------------------------------------------------------------------

CHAIN_DETERMINATION_OUTPUT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "chain_results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "interactor": {"type": "string"},
                    "claim_index": {"type": "integer"},
                    "claim_function_name": {"type": "string"},
                    "chain": {"type": "array", "items": {"type": "string"}},
                    "chain_display": {"type": "string"},
                    "intermediaries": {"type": "array", "items": {"type": "string"}},
                    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                },
                "required": ["interactor", "chain", "intermediaries"],
            },
        },
    },
    "required": ["chain_results"],
}

HIDDEN_INDIRECT_CONFIRMATION_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "confirmations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "interactor": {"type": "string"},
                    "claim_index": {"type": "integer"},
                    "claim_function_name": {"type": "string"},
                    "confirmed_proteins": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Proteins confirmed as genuinely implicated",
                    },
                    "rejected_proteins": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Proteins that are generic references, not mechanistic",
                    },
                    "reasoning": {"type": "string"},
                },
                "required": ["interactor", "claim_index", "confirmed_proteins"],
            },
        },
    },
    "required": ["confirmations"],
}

CLAIM_SIMILARITY_OUTPUT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "comparisons": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "chain_display": {"type": "string"},
                    "new_protein": {"type": "string"},
                    "existing_claim_index": {"type": ["integer", "null"]},
                    "existing_claim_function_name": {"type": ["string", "null"]},
                    "similarity": {
                        "type": "string",
                        "enum": ["identical", "very_similar", "somewhat_similar", "different"],
                    },
                    "is_claim_v": {"type": "boolean"},
                    "reasoning": {"type": "string"},
                },
                "required": ["chain_display", "new_protein", "is_claim_v"],
            },
        },
    },
    "required": ["comparisons"],
}
