/**
 * Type contract: Python `services/data_builder.py:build_full_json_from_db` → TypeScript.
 *
 * Hand-written. Each field documents its source as `@source <python_file>:<symbol>`.
 * Phase 6 may swap to codegen via `openapi-typescript` once we ship `GET /api/types`.
 */

/** A protein symbol used as a key into `useSnapStore.snapshots`. */
export type ProteinKey = string;

/** Arrow class — semantic verb classification. Not exhaustive (LLM emits more). */
export type ArrowClass =
  | "activates"
  | "inhibits"
  | "binds"
  | "regulates"
  | "phosphorylates"
  | "ubiquitinates"
  | "stabilizes"
  | "destabilizes"
  | "induces"
  | "represses"
  | "recruits"
  | "sequesters"
  | "cleaves"
  | "degrades"
  | "deubiquitinates"
  | "is_substrate_of"
  | "is_activated_by"
  | "is_inhibited_by"
  | "is_phosphorylated_by"
  | "is_ubiquitinated_by"
  | "is_degraded_by"
  | "is_cleaved_by"
  | "is_regulated_by"
  | "is_recruited_by"
  | "is_stabilized_by"
  | "is_destabilized_by"
  | "is_repressed_by"
  | string;

/** @source services/data_builder.py:_chain_fields_for */
export interface ChainArrow {
  from: string;
  to: string;
  arrow: ArrowClass;
}

/** @source services/data_builder.py:_chain_fields_for */
export interface ChainSummary {
  chain_id: number;
  role: "origin" | "hop" | "net_effect" | string;
  chain_proteins: string[];
  chain_with_arrows: ChainArrow[];
  pathway_name: string | null;
  /** @source models.py:IndirectChain — query in which this chain was first discovered. */
  discovered_in_query: string;
  /** @source Phase A.1 — distinct pathway_name across all of this chain's claims */
  chain_pathways?: string[];
}

/** @source pipeline/prompts/modern_steps.py + post_processor */
export interface Claim {
  /** Top-level pathway field; can be a string or { canonical_name, name } */
  pathway?: string | { canonical_name?: string; name?: string; hierarchy?: string[] } | null;
  /** Function name (claim title). Garbage names like "__fallback__" classified separately. */
  function?: string | null;
  cellular_process?: string | null;
  effect_description?: string | null;
  biological_consequences?: string[] | string | null;
  specific_effects?: string[] | string | null;
  evidence?: Array<{ pmid?: string; quote?: string; year?: number }> | null;
  pmids?: string[];
  arrow?: ArrowClass;
  /** @source migrations/versions/20260503_0007_function_context_not_null.py — NOT NULL since 2026-05-03. */
  function_context?: "direct" | "indirect" | "chain" | string | null;
  _hierarchy?: string[];
  _synthetic?: boolean;
  _thin_claim?: boolean;
  _synthetic_from_router?: boolean;
  /** @source pipeline/prompts/modern_steps.py — router routing-outcome summary; rendered when `_synthetic_from_router=true`. */
  _router_outcome_summary?: string | null;
  /** @source pipeline depth-validator: list of failing rule names (e.g. min_evidence_papers, min_sentences). */
  _depth_issues?: string[] | null;
  [key: string]: unknown;
}

/** @source services/data_builder.py + 06_FRONTEND_DEEPDIVE.md SNAP.interactions */
export interface Interaction {
  source: string;
  target: string;
  arrow: ArrowClass;
  arrows?: { a_to_b?: ArrowClass[]; b_to_a?: ArrowClass[] };
  direction: "main_to_primary" | "primary_to_main" | "a_to_b" | "b_to_a";
  type: "direct" | "indirect";
  interaction_type: "direct" | "indirect";
  depth: number;
  functions?: Claim[];
  claims?: Claim[];
  pathways?: string[];
  pmids?: string[];

  _is_chain_link?: boolean;
  _chain_position?: number;
  _chain_length?: number;
  _chain_entity?: ChainSummary;
  chain_id?: number | null;
  chain_ids?: number[];
  all_chains?: ChainSummary[];
  /** @source Phase A.1 — distinct pathways across this interaction's chains */
  chain_pathways?: string[];
  mediator_chain?: string[];
  upstream_interactor?: string;
  chain_context?: { full_chain?: string[]; role?: string };

  _source_is_pseudo?: boolean;
  _target_is_pseudo?: boolean;
  _partner_is_pseudo?: boolean;

  [key: string]: unknown;
}

/** @source services/data_builder.py SNAP.pathways */
export interface Pathway {
  id: string;
  name: string;
  description?: string | null;
  hierarchy_level?: number;
  is_leaf?: boolean;
  parent_ids?: string[];
  child_ids?: string[];
  ancestor_ids?: string[];
  interactor_ids?: string[];
  cross_query_interactor_ids?: string[];
  interactions?: Interaction[];
  cross_query_interactions?: Interaction[];
  ontology_id?: string | null;
  ontology_source?: string | null;
}

/** @source services/data_builder.py:build_full_json_from_db diagnostics merge */
export interface Diagnostics {
  pass_rate?: number;
  shallow_funcs?: { count?: number; total?: number; entries?: unknown[] };
  dropped?: { count?: number; entries?: unknown[] };
  unrecoverable?: { count?: number; entries?: unknown[] };
  partial_chains?: { count?: number; entries?: unknown[] };
  drift_corrected?: { count?: number; entries?: unknown[] };
  drift_report_only?: { count?: number; entries?: unknown[] };
  pathway_drifts?: unknown[];
  [key: string]: unknown;
}

/** @source templates/visualize.html SNAP after legacy backfill + freeze */
export interface Snapshot {
  main: string;
  proteins?: string[];
  interactions?: Interaction[];
  pathways?: Pathway[];
  interactors?: unknown[];
  _diagnostics?: Diagnostics;
  _pipeline_status?: string;
  _completed_phases?: string[];
  /** @source Phase 6 plan — schema versioning header */
  _schema_version?: string;
  [key: string]: unknown;
}

/** @source services/data_builder.py ctx_json */
export interface Context {
  [key: string]: unknown;
}

/** @source /api/visualize/<protein> top-level result */
export interface VisualizeApiPayload {
  snapshot_json: Snapshot;
  ctx_json?: Context;
  _diagnostics?: Diagnostics;
  _pipeline_status?: string;
  _completed_phases?: string[];
  _schema_version?: string;
  [key: string]: unknown;
}
