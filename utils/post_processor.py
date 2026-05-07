"""
PostProcessor: Configurable post-processing pipeline for ProPaths payloads.

Consolidates 9 post-processing stages from runner.py into a pluggable,
testable stage chain.  Each stage is described by a StageDescriptor and
wrapped in a thin adapter that normalises to a common signature.
"""

from __future__ import annotations

import enum
import sys
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Stage taxonomy
# ---------------------------------------------------------------------------

class StageKind(enum.Enum):
    PURE = "pure"
    LLM = "llm"
    EXTERNAL_API = "external_api"


@dataclass(frozen=True)
class StageDescriptor:
    """Immutable descriptor for a single post-processing stage."""

    name: str
    label: str
    kind: StageKind
    fn: Callable[..., Dict[str, Any]]
    requires_api_key: bool = False
    default_skip: bool = False
    skip_flag: Optional[str] = None
    critical: bool = False  # If True, failure aborts remaining stages


# ---------------------------------------------------------------------------
# Stage adapters — thin wrappers that normalise diverse signatures
# ---------------------------------------------------------------------------

_VALID_FUNCTION_CONTEXTS = {"direct", "net", "chain_derived", "mixed"}


def _adapt_normalize_function_contexts(payload, *, api_key=None, verbose=False, **kw):
    """Stamp a default ``function_context`` on every function that lacks one.

    The step2a LLM prompt asks the model to return ``function_context`` on
    each function, and ``pipeline/types.py`` marks it as a required schema
    field, but in practice the model drops it unreliably. By the time
    ``db_sync._save_claims`` sees the payload, some functions have a valid
    context and others are missing it entirely — which then surfaces as the
    ``function_context_drift`` verification failure (parent interaction
    says 'direct' but a child claim has NULL context).

    Fix at the source: walk the payload once and stamp every function with
    the right default based on its parent interactor. Rules:

      * ``interaction_type == "direct"`` → default is ``"direct"``.
      * ``interaction_type == "indirect"`` → default is ``"net"``
        (the indirect row is the net-effect record per the dual-track
        docstring in ``models.py:90-94``). Functions the LLM explicitly
        tagged ``"chain_derived"`` keep that tag.

    Functions that already carry a valid context are left untouched — the
    LLM occasionally does include it, and we shouldn't second-guess an
    explicit ``"chain_derived"`` tag.

    Also mirrors the chosen default onto ``interactor["function_context"]``
    so the interaction-level column on the DB row always agrees with its
    claims (which then flow through ``_save_claims`` to ``InteractionClaim``
    via natural parent→child inheritance, no fallback required).

    Pure, no LLM, no DB. Idempotent.
    """
    ctx = payload.get("ctx_json", {})
    interactors = ctx.get("interactors", [])
    if not interactors:
        return payload

    stamped = 0
    for interactor in interactors:
        itype = (interactor.get("interaction_type") or "direct").lower()
        if itype == "indirect":
            default_ctx = "net"
        else:
            default_ctx = "direct"

        for fn in interactor.get("functions", []):
            if not isinstance(fn, dict):
                continue
            existing = fn.get("function_context")
            if existing in _VALID_FUNCTION_CONTEXTS:
                continue
            fn["function_context"] = default_ctx
            stamped += 1

        # Interactor-level label always mirrors the default for its
        # interaction_type at normalize time. A "mixed" rollup only
        # makes sense AFTER chain-link merges add chain_derived claims
        # to a direct interaction — that happens inside ``_save_claims``
        # in db_sync via the per-function ``function_context`` field,
        # and is a separate concern from this normalize pass. At
        # normalize time the functions are all from one step2a batch
        # and share one context by construction, so "mixed" would
        # almost always be a lie.
        interactor["function_context"] = default_ctx

    if stamped and verbose:
        print(
            f"[NORMALIZE CTX] Stamped default function_context on "
            f"{stamped} function entr{'y' if stamped == 1 else 'ies'}",
            file=sys.stderr, flush=True,
        )
    return payload


def _adapt_chain_link_completeness(payload, *, api_key=None, verbose=False, **kw):
    """L4.3 — Audit chain_link_functions completeness BEFORE the DB sync.

    For every indirect interactor with a mediator_chain (or full_chain),
    expect exactly ``len(full_chain) - 1`` entries in chain_link_functions
    (keyed by canonical pair key). Missing hops get tagged on the parent
    interactor under ``_chain_incomplete_hops`` so the frontend can render
    a "(partial)" badge, and structured logging tells operators where the
    holes are.

    This stage is PURE: it only annotates, never deletes data. The Locus
    Router and chain audit at sync time still run normally.
    """
    try:
        from utils.chain_resolution import canonical_pair_key as _cpk
    except Exception:
        return payload  # if chain_resolution can't be imported, skip silently
    ctx = payload.get("ctx_json", {})
    interactors = ctx.get("interactors", []) if isinstance(ctx, dict) else []
    total_missing = 0
    flagged = 0
    for inter in interactors:
        if not isinstance(inter, dict):
            continue
        if inter.get("interaction_type") != "indirect":
            continue
        # Build full_chain from mediator_chain + endpoints
        primary = inter.get("primary")
        mediator = inter.get("mediator_chain") or []
        upstream = inter.get("upstream_interactor")
        main_sym = (ctx.get("main") if isinstance(ctx, dict) else None) or ""
        full_chain = [main_sym] + list(mediator) + [primary]
        full_chain = [p for p in full_chain if p]
        # F4: collapse consecutive duplicates BEFORE iterating hops.
        # Some chain reconstructions emit `mediator_chain=[X]` while
        # `primary=X` (the last mediator IS the primary), which produces
        # `full_chain=[main, X, X]` and a bogus self-hop `X->X` in the
        # audit. The biology here is `main->X` — one hop, not two. Same
        # for accidental `[main, main, X]` shapes when the model echoes
        # the query in mediator_chain.
        deduped_chain: list[str] = []
        for sym in full_chain:
            if not deduped_chain or deduped_chain[-1] != sym:
                deduped_chain.append(sym)
        full_chain = deduped_chain
        if len(full_chain) < 2:
            continue
        clf = inter.get("chain_link_functions") or {}
        if not isinstance(clf, dict):
            continue
        present_keys = {k for k in clf.keys() if isinstance(k, str)}
        missing = []
        for i in range(len(full_chain) - 1):
            src, tgt = full_chain[i], full_chain[i + 1]
            # F4: defensive self-hop guard. Even after the dedup above,
            # if non-adjacent duplicates slip through (X, Y, X) the
            # iteration is fine — only adjacent duplicates produce a
            # self-hop, which is biologically meaningless and never an
            # LLM-emitted hop pair. Skip if it ever appears.
            if src == tgt:
                continue
            # Canonical pair key (alphabetical|pipe-separated). Also accept
            # the legacy directional ``A->B`` form so payloads emitted by
            # older prompt rounds are not flagged falsely.
            canon = _cpk(src, tgt)
            legacy_fwd = f"{src}->{tgt}"
            legacy_rev = f"{tgt}->{src}"
            if canon in present_keys or legacy_fwd in present_keys or legacy_rev in present_keys:
                continue
            missing.append(f"{src}->{tgt}")
        if missing:
            inter["_chain_incomplete_hops"] = missing
            total_missing += len(missing)
            flagged += 1
    if flagged:
        print(
            f"[CHAIN AUDIT] {flagged} indirect interactor(s) missing "
            f"{total_missing} hop entr{'y' if total_missing == 1 else 'ies'} "
            f"in chain_link_functions; flagged with _chain_incomplete_hops "
            f"so the frontend renders a (partial) badge.",
            file=sys.stderr,
        )
    return payload


def _adapt_chain_group_tagging(payload, *, api_key=None, verbose=False, **kw):
    """Tag function entries with their chain group for batched verification.

    Reads _chain_annotations_explicit and _chain_annotations_hidden from
    ctx_json and sets a '_chain_group' key on every function entry that
    participates in a resolved chain.  Downstream LLM stages (evidence
    validation, citation finder) can then batch chain-grouped claims
    together so they are analyzed with shared context and papers.
    """
    ctx = payload.get("ctx_json", {})
    interactors = {i.get("primary", "").upper(): i for i in ctx.get("interactors", [])}

    def _parse(chain):
        if isinstance(chain, list):
            return [p.strip().strip("^*") for p in chain]
        return [p.strip().strip("^*") for p in chain.replace("→", "->").split("->")]

    group_id = 0
    for annot_key in ("_chain_annotations_explicit", "_chain_annotations_hidden"):
        for entry in ctx.get(annot_key, []):
            chain = _parse(entry.get("chain", []))
            if len(chain) < 2:
                continue
            group_label = f"chain_{group_id}_{' -> '.join(chain)}"
            group_id += 1
            # Tag all functions on all interactors in this chain
            for protein in chain:
                i = interactors.get(protein.upper())
                if i:
                    for fn in i.get("functions", []):
                        fn["_chain_group"] = group_label

    tagged = sum(
        1 for i in ctx.get("interactors", [])
        for fn in i.get("functions", [])
        if fn.get("_chain_group")
    )
    if tagged and verbose:
        print(f"[CHAIN TAGGING] Tagged {tagged} function entries with chain groups", file=sys.stderr)
    return payload


def _adapt_schema_pre_gate(payload, *, api_key=None, verbose=False, **kw):
    from utils.schema_validator import validate_schema_consistency
    return validate_schema_consistency(
        payload, fix_arrows=True, fix_chains=True, fix_directions=True, verbose=verbose,
    )


def _adapt_dedup_functions(payload, *, api_key=None, verbose=False, **kw):
    from utils.deduplicate_functions import deduplicate_payload
    return deduplicate_payload(payload, api_key, verbose=verbose)


def _adapt_evidence_validation(payload, *, api_key=None, verbose=False, step_logger=None, **kw):
    from utils.evidence_validator import validate_and_enrich_evidence
    return validate_and_enrich_evidence(payload, api_key, verbose=verbose, step_logger=step_logger)



def _adapt_interaction_metadata(payload, *, api_key=None, verbose=False, **kw):
    from utils.interaction_metadata_generator import generate_interaction_metadata
    return generate_interaction_metadata(payload, verbose=verbose, api_key=api_key)



def _adapt_citation_finder(payload, *, api_key=None, verbose=False, **kw):
    from utils.citation_finder import find_and_verify_citations
    return find_and_verify_citations(payload, api_key, verbose=verbose)


def _adapt_arrow_validation(payload, *, api_key=None, verbose=False, flask_app=None, **kw):
    from utils.arrow_effect_validator import validate_arrows_for_payload
    return validate_arrows_for_payload(payload, api_key, verbose=verbose, flask_app=flask_app)


def _adapt_clean_function_names(payload, *, verbose=False, **kw):
    from utils.clean_function_names import clean_payload_function_names
    return clean_payload_function_names(payload, verbose=verbose)


def _adapt_finalize_metadata(payload, *, verbose=False, **kw):
    from utils.schema_validator import finalize_interaction_metadata
    return finalize_interaction_metadata(
        payload, add_arrow_notation=True, validate_snapshot=True, verbose=verbose,
    )


def _adapt_quality_validation(payload, *, user_query=None, verbose=False, **kw):
    """Runtime enforcement of the PhD-level depth prompt directive.

    Counts sentences per function mechanism (``cellular_process``) and
    cascades (``biological_consequence``) and tags any function outside
    the 6-10 / 3-5 ranges with ``_depth_issues``. Writes a structured
    report to ``Logs/<protein>/quality_report.json``. Pure — no LLM or
    network. Non-fatal — never raises, just tags.

    Also stashes a compact ``quality_report`` summary on
    ``payload["_pipeline_metadata"]`` so the API response can surface
    pass_rate without re-reading the JSON file from disk.
    """
    from utils.quality_validator import run_quality_validation
    protein_symbol = (user_query or payload.get("ctx_json", {}).get("main", "UNKNOWN"))
    out, report = run_quality_validation(payload, protein_symbol=protein_symbol, verbose=verbose)
    # Surface the depth report to downstream consumers (API response →
    # frontend banner). Keep just the summary fields, not the full
    # violations list, to avoid bloating the JSON. Full violations live
    # in Logs/<protein>/quality_report.json for deeper inspection.
    out.setdefault("_pipeline_metadata", {})["quality_report"] = {
        "total_functions": report.total_functions,
        "flagged_functions": report.flagged_functions,
        "pass_rate": round(report.pass_rate, 3),
        "thresholds": {
            "min_sentences": 6, "max_sentences": 10,
            "min_cascades": 3, "max_cascades": 5,
        },
    }
    return out


# ---------------------------------------------------------------------------
# Default stage chain builder
# ---------------------------------------------------------------------------

def _build_default_stages() -> List[StageDescriptor]:
    """Build the default post-processing stage chain, omitting unavailable modules."""
    stages: List[StageDescriptor] = []

    # 0 — Chain group tagging (must run before any LLM verification stage)
    stages.append(StageDescriptor(
        name="chain_group_tagging",
        label="Tagging chain-grouped claims...",
        kind=StageKind.PURE,
        fn=_adapt_chain_group_tagging,
        skip_flag="skip_chain_tagging",
    ))

    # 0a — L4.3: chain_link_functions completeness gate. Detect indirect
    # interactors whose chain is missing per-hop entries and tag them so
    # the frontend can render "(partial)" instead of silently swallowing
    # missing biology. Pure annotation; never destroys data.
    stages.append(StageDescriptor(
        name="chain_link_completeness",
        label="Auditing chain_link_functions completeness...",
        kind=StageKind.PURE,
        fn=_adapt_chain_link_completeness,
        skip_flag="skip_chain_link_completeness",
    ))

    # 0b — Normalize function_context on every function before anything
    # downstream reads it. Eliminates the function_context_drift root
    # cause: once this stage has run, ``_save_claims`` can trust that
    # every function's ``function_context`` is a valid, non-null value
    # and no fallback / inheritance hack is needed at write time.
    stages.append(StageDescriptor(
        name="normalize_function_contexts",
        label="Normalizing function contexts...",
        kind=StageKind.PURE,
        fn=_adapt_normalize_function_contexts,
        skip_flag="skip_normalize_function_contexts",
    ))

    # 1 — Schema pre-gate
    try:
        from utils.schema_validator import validate_schema_consistency  # noqa: F401
        stages.append(StageDescriptor(
            name="schema_pre_gate",
            label="Validating data consistency...",
            kind=StageKind.PURE,
            fn=_adapt_schema_pre_gate,
            skip_flag="skip_schema_validation",
        ))
    except ImportError as _ie:
        print(f"[WARN] Post-processor stage skipped (import failed): {_ie}", file=sys.stderr)

    # 2 — Arrow validation + direct link extraction
    # Runs BEFORE deduplication AND evidence validation so that:
    #   • Dedup compares functions with corrected arrows (otherwise it
    #     would group two semantically-identical mechanisms across an
    #     uncorrected arrow disagreement and pick the wrong winner).
    #   • Evidence text is generated against corrected arrows, not
    #     pre-correction arrows.
    # Was: dedup → arrow. Swapped on 2026-04-29 to fix arrow-blind dedup
    # decisions noted in the audit.
    try:
        from utils.arrow_effect_validator import validate_arrows_for_payload  # noqa: F401
        stages.append(StageDescriptor(
            name="arrow_validation",
            label="Validating arrows & extracting direct links...",
            kind=StageKind.LLM,
            fn=_adapt_arrow_validation,
            requires_api_key=True,
            skip_flag="skip_arrow_validation",
            critical=True,
        ))
    except ImportError as _ie:
        print(f"[WARN] Post-processor stage skipped (import failed): {_ie}", file=sys.stderr)

    # 3 — Deduplicate functions (after arrow validation has corrected arrows)
    try:
        from utils.deduplicate_functions import deduplicate_payload  # noqa: F401
        stages.append(StageDescriptor(
            name="dedup_functions",
            label="Deduplicating functions...",
            kind=StageKind.LLM,
            fn=_adapt_dedup_functions,
            requires_api_key=True,
            skip_flag="skip_deduplicator",
        ))
    except ImportError as _ie:
        print(f"[WARN] Post-processor stage skipped (import failed): {_ie}", file=sys.stderr)

    # 4 — Evidence validation (now runs after arrow validation for consistency)
    try:
        from utils.evidence_validator import validate_and_enrich_evidence  # noqa: F401
        stages.append(StageDescriptor(
            name="evidence_validation",
            label="Validating & enriching evidence...",
            kind=StageKind.LLM,
            fn=_adapt_evidence_validation,
            requires_api_key=True,
            skip_flag="skip_validation",
        ))
    except ImportError as _ie:
        print(f"[WARN] Post-processor stage skipped (import failed): {_ie}", file=sys.stderr)

    # 5 — Citation finder (Gemini + NCBI verification)
    # Skipped by default: step2e_citation_verification already runs during
    # the main pipeline, making this redundant.  Set skip_pmid_update=False
    # or use requery mode to re-enable.
    try:
        from utils.citation_finder import find_and_verify_citations  # noqa: F401
        stages.append(StageDescriptor(
            name="update_pmids",
            label="Finding & verifying citations...",
            kind=StageKind.LLM,
            fn=_adapt_citation_finder,
            requires_api_key=True,
            default_skip=True,
            skip_flag="skip_pmid_update",
        ))
    except ImportError as _ie:
        print(f"[WARN] Post-processor stage skipped (import failed): {_ie}", file=sys.stderr)

    # 7 — Interaction metadata (runs after arrow validation for consistency)
    try:
        from utils.interaction_metadata_generator import generate_interaction_metadata  # noqa: F401
        stages.append(StageDescriptor(
            name="interaction_metadata",
            label="Analyzing interaction patterns...",
            kind=StageKind.LLM,
            fn=_adapt_interaction_metadata,
            skip_flag="skip_interaction_metadata",
            requires_api_key=True,
        ))
    except ImportError as _ie:
        print(f"[WARN] Post-processor stage skipped (import failed): {_ie}", file=sys.stderr)

    # 9 — Clean function names (catches functions produced by post-processing stages)
    try:
        from utils.clean_function_names import clean_payload_function_names  # noqa: F401
        stages.append(StageDescriptor(
            name="clean_function_names",
            label="Normalizing function names...",
            kind=StageKind.PURE,
            fn=_adapt_clean_function_names,
            skip_flag="skip_clean_names",
        ))
    except ImportError as _ie:
        print(f"[WARN] Post-processor stage skipped (import failed): {_ie}", file=sys.stderr)

    # 9b — PhD-level depth validation (runtime enforcement of the prompt
    # directive). Pure, non-fatal — only tags and writes a report.
    try:
        from utils.quality_validator import run_quality_validation  # noqa: F401
        stages.append(StageDescriptor(
            name="quality_validation",
            label="Validating PhD-level depth (6-10 sentences / 3-5 cascades)...",
            kind=StageKind.PURE,
            fn=_adapt_quality_validation,
            skip_flag="skip_quality_validation",
        ))
    except ImportError as _ie:
        print(f"[WARN] Post-processor stage skipped (import failed): {_ie}", file=sys.stderr)

    # 10 — Finalize metadata (note: was stage 12, renumbered after removing deprecated stages)
    try:
        from utils.schema_validator import finalize_interaction_metadata  # noqa: F401
        stages.append(StageDescriptor(
            name="finalize_metadata",
            label="Finalizing interaction metadata...",
            kind=StageKind.PURE,
            fn=_adapt_finalize_metadata,
            skip_flag="skip_finalize_metadata",
        ))
    except ImportError as _ie:
        print(f"[WARN] Post-processor stage skipped (import failed): {_ie}", file=sys.stderr)

    return stages


def _build_requery_stages() -> List[StageDescriptor]:
    """Build the requery-specific stage chain.

    Requery runs a subset of stages in a different order than the full job:
    evidence_validation → interaction_metadata → update_pmids → dedup_functions.

    No schema gate, arrow validation, clean names, finalize, or pathway.
    S1: bidirectional_split removed — bidirectional is dead.
    """
    stages: List[StageDescriptor] = []

    # Evidence validation
    try:
        from utils.evidence_validator import validate_and_enrich_evidence  # noqa: F401
        stages.append(StageDescriptor(
            name="evidence_validation",
            label="Validating new evidence...",
            kind=StageKind.LLM,
            fn=_adapt_evidence_validation,
            requires_api_key=True,
        ))
    except ImportError as _ie:
        print(f"[WARN] Post-processor stage skipped (import failed): {_ie}", file=sys.stderr)

    # Interaction metadata
    try:
        from utils.interaction_metadata_generator import generate_interaction_metadata  # noqa: F401
        stages.append(StageDescriptor(
            name="interaction_metadata",
            label="Analyzing interaction patterns...",
            kind=StageKind.LLM,
            fn=_adapt_interaction_metadata,
            requires_api_key=True,
        ))
    except ImportError as _ie:
        print(f"[WARN] Post-processor stage skipped (import failed): {_ie}", file=sys.stderr)

    # Citation finder (Gemini + NCBI verification)
    try:
        from utils.citation_finder import find_and_verify_citations  # noqa: F401
        stages.append(StageDescriptor(
            name="update_pmids",
            label="Finding & verifying citations...",
            kind=StageKind.LLM,
            fn=_adapt_citation_finder,
            requires_api_key=True,
        ))
    except ImportError as _ie:
        print(f"[WARN] Post-processor stage skipped (import failed): {_ie}", file=sys.stderr)

    # Dedup functions (after PMID update in requery)
    try:
        from utils.deduplicate_functions import deduplicate_payload  # noqa: F401
        stages.append(StageDescriptor(
            name="dedup_functions",
            label="Deduplicating functions...",
            kind=StageKind.LLM,
            fn=_adapt_dedup_functions,
            requires_api_key=True,
            skip_flag="skip_deduplicator",
        ))
    except ImportError as _ie:
        print(f"[WARN] Post-processor stage skipped (import failed): {_ie}", file=sys.stderr)

    return stages


# ---------------------------------------------------------------------------
# PostProcessor
# ---------------------------------------------------------------------------

class PostProcessor:
    """Configurable post-processing pipeline for ProPaths payloads."""

    def __init__(
        self,
        stages: Optional[List[StageDescriptor]] = None,
        skip_flags: Optional[Dict[str, bool]] = None,
    ):
        self._stages = stages if stages is not None else _build_default_stages()
        self._skip_flags = skip_flags or {}

    # -- public helpers -----------------------------------------------------

    @staticmethod
    def default_stages() -> List[StageDescriptor]:
        """Return the standard 9-stage chain (availability-aware)."""
        return _build_default_stages()

    @staticmethod
    def requery_stages() -> List[StageDescriptor]:
        """Return the requery-specific subset chain."""
        return _build_requery_stages()

    def active_stages(self) -> List[StageDescriptor]:
        """Return only stages that should actually run given current flags."""
        active: List[StageDescriptor] = []
        for stage in self._stages:
            if stage.default_skip:
                continue
            if stage.skip_flag and self._skip_flags.get(stage.skip_flag, False):
                continue
            active.append(stage)
        return active

    def skipped_stage_names(self) -> List[str]:
        """L3.5 — Return the names of stages that *would* be skipped, so the
        caller (and downstream API responses) can flag user-visible
        consequences (e.g. unverified citations).
        """
        return [
            stage.name
            for stage in self._stages
            if stage.default_skip
            or (stage.skip_flag and self._skip_flags.get(stage.skip_flag, False))
        ]

    def count_steps(self) -> int:
        """Count active stages (for progress-bar total_steps calculation)."""
        return len(self.active_stages())

    # -- main entry point ---------------------------------------------------

    def run(
        self,
        payload: Dict[str, Any],
        *,
        api_key: Optional[str] = None,
        user_query: Optional[str] = None,
        flask_app: Optional[Any] = None,
        step_logger: Optional[Any] = None,
        update_status: Optional[Callable] = None,
        current_step: int = 0,
        total_steps: int = 0,
        consume_metrics: Optional[Callable] = None,
        verbose: bool = False,
    ) -> Tuple[Dict[str, Any], int]:
        """Run all active stages sequentially.

        Returns:
            (payload, final_step_number) — the transformed payload and the
            updated step counter for progress tracking.
        """
        skipped_stages: list[str] = []
        active = self.active_stages()
        total_active = len(active)
        for stage_idx, stage in enumerate(active):
            if stage.requires_api_key and not api_key:
                print(f"[WARN] Skipping stage '{stage.name}': no API key", file=sys.stderr)
                skipped_stages.append(stage.name)
                continue

            # Clear banner for every stage
            print(
                f"\n{'─'*60}\n"
                f"  POST-PROCESSING [{stage_idx + 1}/{total_active}]: {stage.label}\n"
                f"  (stage: {stage.name}, kind: {stage.kind.name})\n"
                f"{'─'*60}",
                flush=True,
            )

            current_step += 1
            if update_status:
                update_status(
                    text=stage.label,
                    current_step=current_step,
                    total_steps=total_steps,
                )

            # Retry each stage up to 4 times (1 initial + 3 retries) with backoff.
            # Only retry transient errors (network, rate limit, DB connection).
            _TRANSIENT_TYPES = (TimeoutError, ConnectionError, OSError)
            try:
                from sqlalchemy.exc import OperationalError as _SAOpError
                _TRANSIENT_TYPES = (*_TRANSIENT_TYPES, _SAOpError)
            except ImportError:
                pass
            last_exc = None
            stage_succeeded = False
            for attempt in range(4):
                try:
                    payload = stage.fn(
                        payload,
                        api_key=api_key,
                        verbose=verbose,
                        step_logger=step_logger,
                        user_query=user_query,
                        flask_app=flask_app,
                    )
                    import time as _time_mod
                    _stage_elapsed = _time_mod.time()
                    print(f"  [OK] {stage.name} complete", flush=True)
                    stage_succeeded = True
                    break
                except _TRANSIENT_TYPES as exc:
                    last_exc = exc
                    if attempt < 3:
                        wait = 2 ** attempt
                        print(
                            f"[WARN] Stage '{stage.name}' transient error (attempt {attempt + 1}/4), "
                            f"retrying in {wait}s: {exc}",
                            file=sys.stderr, flush=True,
                        )
                        import time
                        time.sleep(wait)
                except Exception as exc:
                    # Permanent error (schema, validation, logic) — don't retry
                    last_exc = exc
                    print(
                        f"[ERROR] Stage '{stage.name}' permanent error (no retry): {exc}",
                        file=sys.stderr, flush=True,
                    )
                    break

            if not stage_succeeded and last_exc is not None:
                # Stage failed — log and continue to next stage. This covers
                # both transient exhaustion and permanent no-retry failures.
                print(
                    f"[ERROR] Stage '{stage.name}' failed: {last_exc}",
                    file=sys.stderr, flush=True,
                )
                payload.setdefault("_pipeline_metadata", {}).setdefault(
                    "failed_stages", []
                ).append({"stage": stage.name, "error": str(last_exc)})
                # Circuit breaker: abort if a critical stage fails
                if stage.critical:
                    print(
                        f"[ABORT] Critical stage '{stage.name}' failed — "
                        f"aborting post-processing to prevent corrupted data",
                        file=sys.stderr, flush=True,
                    )
                    payload["_pipeline_aborted"] = (
                        f"Critical stage '{stage.name}' failed: {last_exc}"
                    )
                    break

            if consume_metrics and stage.kind in (StageKind.LLM, StageKind.EXTERNAL_API):
                consume_metrics(payload)

        if skipped_stages:
            payload.setdefault("_pipeline_metadata", {})["skipped_stages"] = skipped_stages

        return payload, current_step
