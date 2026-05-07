"""Results blueprint: /api/results, /api/pathway, /api/databased, /api/expand/*."""

import json
import os
import sys
import threading
from pathlib import Path

from flask import Blueprint, request, jsonify, send_from_directory

from models import db
from services.state import jobs, jobs_lock, CACHE_DIR, PRUNED_DIR
from services.data_builder import build_full_json_from_db, build_expansion_json_from_db, build_protein_detail_json
from services.error_helpers import error_response, ErrorCode
from utils.pruner import (
    run_prune_job,
    pruned_filename,
    is_pruned_fresh,
    make_prune_job_id,
    parse_prune_job_id,
    HARD_MAX_KEEP_DEFAULT,
    PROTEIN_RE,
)
from utils.interaction_contract import (
    normalize_arrow,
    normalize_arrows_map,
    semantic_claim_direction,
)

results_bp = Blueprint('results', __name__)


# H5: ``_get_api_key`` is gone. The pipeline runs on Vertex AI with
# Application Default Credentials (ADC); ``get_client`` in
# ``utils.gemini_runtime`` ignores any api_key argument and pulls
# project/location from ``GOOGLE_CLOUD_PROJECT`` / ``GOOGLE_CLOUD_LOCATION``.
# The pruner gates its own LLM path on ``validate_vertex_config()``,
# so the route doesn't need to thread an api_key through.


@results_bp.route('/api/results/<protein>')
def get_results(protein):
    """Serve complete JSON data for a protein from PostgreSQL."""
    if not PROTEIN_RE.match(protein):
        return error_response("Invalid protein name", ErrorCode.INVALID_INPUT)
    try:
        from utils.protein_aliases import canonicalize_protein_name
        protein = canonicalize_protein_name(protein) or protein
        result = build_full_json_from_db(protein)
        if result:
            return jsonify(result)
        else:
            return error_response("Protein not found", ErrorCode.NOT_FOUND, 404)
    except Exception as e:
        print(f"Database query failed: {e}", file=sys.stderr)
        return error_response("Database query failed", ErrorCode.INTERNAL, 500)


@results_bp.route('/api/chain/<int:chain_id>')
def get_chain(chain_id):
    """Return the canonical IndirectChain row by id.

    The frontend used to read chain shape from each interaction's
    ``data.chain_context`` JSONB blob, which meant a 4-protein chain
    produced 4 redundant copies that could drift. After refactor #6,
    every participating interaction stores only ``chain_id`` (a FK to
    this row), and this endpoint serves the chain definition once.

    Response shape:

    .. code-block:: json

        {
          "chain_id": 42,
          "chain_proteins": ["ATXN3", "VCP", "LAMP2"],
          "chain_with_arrows": [
            {"from": "ATXN3", "to": "VCP",   "arrow": "binds"},
            {"from": "VCP",   "to": "LAMP2", "arrow": "activates"}
          ],
          "discovered_in_query": "ATXN3",
          "pathway": {
            "id": 17,
            "name": "Autophagy-Lysosomal Pathway"
          },
          "origin_interaction_id": 9999,
          "participants": [
            {"id": 100, "protein_a": "ATXN3", "protein_b": "VCP",   "interaction_type": "direct"},
            {"id": 101, "protein_a": "VCP",   "protein_b": "LAMP2", "interaction_type": "direct"},
            {"id": 102, "protein_a": "ATXN3", "protein_b": "LAMP2", "interaction_type": "indirect"}
          ]
        }

    Returns 404 when no chain exists with that id.
    """
    try:
        from models import IndirectChain
        chain = db.session.get(IndirectChain, int(chain_id))
        if not chain:
            return error_response("Chain not found", ErrorCode.NOT_FOUND, 404)

        # Serialize participants via the chain_id back-reference (#6).
        # Use the lazy='dynamic' query so we don't load every related
        # row when the caller only wants the chain definition.
        participants_payload = []
        try:
            for inter in chain.participants:
                a_sym = inter.protein_a.symbol if inter.protein_a else None
                b_sym = inter.protein_b.symbol if inter.protein_b else None
                participants_payload.append({
                    "id": inter.id,
                    "protein_a": a_sym,
                    "protein_b": b_sym,
                    "interaction_type": inter.interaction_type,
                    "arrow": inter.arrow,
                    "direction": inter.direction,
                })
        except Exception as participants_exc:
            # If the lazy relationship fails for any reason, fall back to
            # an empty list rather than 500-ing — the chain definition
            # itself is still useful to the caller.
            print(
                f"[CHAIN API] Could not load participants for chain "
                f"{chain.id}: {type(participants_exc).__name__}: {participants_exc}",
                file=sys.stderr,
            )

        return jsonify({
            "chain_id": chain.id,
            "chain_proteins": list(chain.chain_proteins or []),
            "chain_with_arrows": list(chain.chain_with_arrows or []) if chain.chain_with_arrows else [],
            "discovered_in_query": chain.discovered_in_query,
            "pathway": (
                {
                    "id": chain.pathway_id,
                    "name": chain.pathway_name,
                }
                if chain.pathway_id or chain.pathway_name
                else None
            ),
            "origin_interaction_id": chain.origin_interaction_id,
            "participants": participants_payload,
            "chain_length": len(chain.chain_proteins or []),
        })
    except ValueError:
        return error_response("Invalid chain id", ErrorCode.INVALID_INPUT)
    except Exception as e:
        print(f"[CHAIN API] Failed to fetch chain {chain_id}: {e}", file=sys.stderr)
        return error_response("Chain fetch failed", ErrorCode.INTERNAL, 500)


@results_bp.route('/api/pathway/<pathway_id>/interactors')
def get_pathway_interactors(pathway_id):
    """Lazy-load interactors for a leaf pathway."""
    if not pathway_id or len(pathway_id) > 200:
        return error_response("Invalid pathway identifier", ErrorCode.INVALID_INPUT)
    try:
        from models import Pathway, PathwayInteraction, Interaction, Protein
        from sqlalchemy.orm import joinedload

        pathway_name = pathway_id
        if pathway_name.startswith("pathway_"):
            pathway_name = pathway_name[8:]
        pathway_name = pathway_name.replace("_", " ")

        pathway = Pathway.query.filter_by(name=pathway_name).first()
        if not pathway:
            return error_response(f"Pathway not found: {pathway_name}", ErrorCode.NOT_FOUND, 404)

        pw_interactions = (
            PathwayInteraction.query
            .filter_by(pathway_id=pathway.id)
            .options(
                joinedload(PathwayInteraction.interaction)
                .joinedload(Interaction.protein_a),
                joinedload(PathwayInteraction.interaction)
                .joinedload(Interaction.protein_b),
            )
            .all()
        )

        interactors = []
        seen_symbols = set()

        for pwi in pw_interactions:
            interaction = pwi.interaction
            if not interaction:
                continue

            protein_a = interaction.protein_a
            protein_b = interaction.protein_b

            for protein in [protein_a, protein_b]:
                if protein and protein.symbol not in seen_symbols:
                    seen_symbols.add(protein.symbol)
                    interactors.append({
                        "symbol": protein.symbol,
                        "confidence": float(pwi.assignment_confidence) if pwi.assignment_confidence else 0.8,
                        "interaction_data": interaction.data
                    })

        return jsonify({
            "pathway_id": pathway_id,
            "pathway_name": pathway.name,
            "hierarchy_level": pathway.hierarchy_level or 0,
            "is_leaf": pathway.is_leaf if pathway.is_leaf is not None else True,
            "interactors": interactors
        })
    except Exception as e:
        print(f"[ERROR] get_pathway_interactors: {e}", file=sys.stderr)
        return error_response("Database query failed", ErrorCode.INTERNAL, 500)


@results_bp.route('/api/claims/<protein>')
def get_claims(protein):
    """Get all interaction claims for a protein, each as an independent entry."""
    if not PROTEIN_RE.match(protein):
        return error_response("Invalid protein name", ErrorCode.INVALID_INPUT)
    try:
        from models import Protein, Interaction, InteractionClaim
        from utils.protein_aliases import canonicalize_protein_name
        protein = canonicalize_protein_name(protein) or protein

        target = Protein.query.filter_by(symbol=protein).first()
        if not target:
            return error_response("Protein not found", ErrorCode.NOT_FOUND, 404)

        interactions = Interaction.query.filter(
            (Interaction.protein_a_id == target.id) |
            (Interaction.protein_b_id == target.id)
        ).all()

        # Batch-load all claims for these interactions (avoids N+1)
        interaction_ids = [ix.id for ix in interactions]
        all_claims = InteractionClaim.query.filter(
            InteractionClaim.interaction_id.in_(interaction_ids)
        ).all() if interaction_ids else []
        claims_by_ix = {}
        for c in all_claims:
            claims_by_ix.setdefault(c.interaction_id, []).append(c)

        result = []
        for ix in interactions:
            partner = ix.protein_b if ix.protein_a_id == target.id else ix.protein_a
            claims = claims_by_ix.get(ix.id, [])
            result.append({
                "interaction_id": ix.id,
                "partner": partner.symbol,
                "arrow": normalize_arrow(ix.arrow, default="binds"),
                "direction": ix.direction,
                "interaction_type": ix.interaction_type,
                "depth": ix.depth,
                "claims_count": len(claims),
                "claims": [
                    {
                        "id": c.id,
                        "function_name": c.function_name,
                        "arrow": normalize_arrow(c.arrow, default="regulates"),
                        "interaction_effect": c.interaction_effect,
                        "direction": semantic_claim_direction(c.direction),
                        "mechanism": c.mechanism,
                        "effect_description": c.effect_description,
                        "biological_consequences": c.biological_consequences or [],
                        "specific_effects": c.specific_effects or [],
                        "evidence": c.evidence or [],
                        "pmids": c.pmids or [],
                        "pathway_name": c.pathway_name,
                        "confidence": float(c.confidence) if c.confidence else None,
                    }
                    for c in claims
                ],
            })

        total_claims = sum(len(i["claims"]) for i in result)
        return jsonify({
            "protein": protein,
            "total_interactions": len(result),
            "total_claims": total_claims,
            "interactions": result,
        })
    except Exception as e:
        print(f"[ERROR] get_claims: {e}", file=sys.stderr)
        return error_response("Database query failed", ErrorCode.INTERNAL, 500)


@results_bp.route('/api/databased-interactors/<protein>')
def get_databased_interactors(protein):
    """Fetch previously databased interactors for a protein from OTHER queries."""
    if not PROTEIN_RE.match(protein):
        return error_response("Invalid protein name", ErrorCode.INVALID_INPUT)
    try:
        from models import Protein, Interaction
        from sqlalchemy.orm import joinedload
        from utils.protein_aliases import canonicalize_protein_name
        protein = canonicalize_protein_name(protein) or protein

        exclude_query = request.args.get('exclude_query', '').strip()
        try:
            limit = min(int(request.args.get('limit', 10)), 30)
        except (TypeError, ValueError):
            limit = 10

        target_protein = Protein.query.filter_by(symbol=protein).first()
        if not target_protein:
            return jsonify({"protein": protein, "databased_interactions": []})

        interactions = (
            db.session.query(Interaction)
            .options(
                joinedload(Interaction.protein_a),
                joinedload(Interaction.protein_b)
            )
            .filter(
                (Interaction.protein_a_id == target_protein.id) |
                (Interaction.protein_b_id == target_protein.id)
            )
            .limit(limit + 20)
            .all()
        )

        exclude_protein = None
        if exclude_query:
            exclude_protein = Protein.query.filter_by(symbol=exclude_query).first()

        results = []
        seen_partners = set()

        for interaction in interactions:
            if interaction.protein_a_id == target_protein.id:
                partner = interaction.protein_b
            else:
                partner = interaction.protein_a

            if not partner:
                continue
            if exclude_protein and partner.id == exclude_protein.id:
                continue
            if partner.symbol in seen_partners:
                continue
            seen_partners.add(partner.symbol)

            # C1: depth through the canonical ChainView (empty view for
            # direct rows → 0 → falls through ``or 1`` to the depth=1
            # default). No direct read of interaction.depth.
            # C2: arrow through the canonical primary_arrow property +
            # emit the full arrows dict for multi-type support.
            results.append({
                "partner": partner.symbol,
                "arrow": normalize_arrow(interaction.primary_arrow, default="binds"),
                "arrows": normalize_arrows_map(interaction.arrows),
                "direction": interaction.direction or "main_to_primary",
                "discovered_in_query": interaction.discovered_in_query or "unknown",
                "confidence": float(interaction.confidence) if interaction.confidence else 0.5,
                "interaction_type": interaction.interaction_type or "direct",
                "depth": interaction.chain_view.depth or 1,
            })

            if len(results) >= limit:
                break

        return jsonify({
            "protein": protein,
            "databased_interactions": results
        })
    except Exception as e:
        print(f"[ERROR] get_databased_interactors: {e}", file=sys.stderr)
        return error_response("Database query failed", ErrorCode.INTERNAL, 500)


@results_bp.route('/api/protein/<symbol>/interactions')
def get_protein_interactions(symbol):
    """Get ALL interactions for a protein from the database (for modal detail view)."""
    if not PROTEIN_RE.match(symbol):
        return error_response("Invalid protein name", ErrorCode.INVALID_INPUT)
    try:
        from utils.protein_aliases import canonicalize_protein_name
        symbol = canonicalize_protein_name(symbol) or symbol
        result = build_protein_detail_json(symbol)
        if result is not None:
            return jsonify(result)
        return error_response("Protein not found", ErrorCode.NOT_FOUND, 404)
    except Exception as e:
        print(f"[ERROR] get_protein_interactions({symbol}): {e}", file=sys.stderr)
        return error_response("Database query failed", ErrorCode.INTERNAL, 500)


@results_bp.post('/api/expand/pruned')
def expand_pruned():
    """Request a pruned subgraph for an expanded interactor with auto-cross-linking."""
    data = request.get_json(silent=True) or {}
    parent = (data.get("parent") or "").strip()
    protein = (data.get("protein") or "").strip()
    current_nodes = data.get("current_nodes") or []
    visible_proteins = data.get("visible_proteins") or []
    parent_edge = data.get("parent_edge") or {}
    try:
        max_keep = int(data.get("max_keep") or HARD_MAX_KEEP_DEFAULT)
    except (TypeError, ValueError):
        max_keep = HARD_MAX_KEEP_DEFAULT
    max_keep = min(max_keep, HARD_MAX_KEEP_DEFAULT)

    if not parent or not PROTEIN_RE.match(parent):
        return error_response("Invalid parent", ErrorCode.INVALID_INPUT)
    if not protein or not PROTEIN_RE.match(protein):
        return error_response("Invalid protein", ErrorCode.INVALID_INPUT)

    # Try to build from PostgreSQL database first (with cross-linking support)
    try:
        from models import Protein
        protein_in_db = Protein.query.filter_by(symbol=protein).first()
        if protein_in_db and protein_in_db.total_interactions > 0:
            full_path = os.path.join(CACHE_DIR, f"{protein}.json")
            pruned_name = pruned_filename(parent, protein)
            pruned_path = os.path.join(PRUNED_DIR, pruned_name)
            job_id = make_prune_job_id(parent, protein)

            if os.path.exists(full_path) and is_pruned_fresh(Path(full_path), Path(pruned_path), hard_max_keep=max_keep):
                with jobs_lock:
                    jobs[job_id] = {"status": "complete"}
                print(f"[PRUNE CACHE HIT] Using cached pruned data for {protein}", file=sys.stderr)
                return jsonify({"status": "complete", "job_id": job_id}), 200

            expansion_data = build_expansion_json_from_db(protein, visible_proteins)
            if expansion_data:
                with open(full_path, 'w', encoding='utf-8') as f:
                    json.dump(expansion_data, f, indent=2, ensure_ascii=False)

                def _run():
                    try:
                        run_prune_job(
                            full_json_path=Path(full_path),
                            pruned_json_path=Path(pruned_path),
                            parent=parent,
                            current_nodes=current_nodes,
                            parent_edge=parent_edge,
                            hard_max_keep=max_keep,
                            use_llm=False,
                        )
                        with jobs_lock:
                            jobs[job_id] = {"status": "complete"}
                    except Exception as e:
                        with jobs_lock:
                            jobs[job_id] = {"status": "error", "error": str(e)}

                with jobs_lock:
                    jobs[job_id] = {"status": "processing", "text": "Pruning subgraph with cross-links..."}
                t = threading.Thread(target=_run, daemon=True)
                t.start()
                return jsonify({"status": "queued", "job_id": job_id}), 202
    except Exception as e:
        print(f"[WARN]Database expansion failed, falling back to file cache: {e}", file=sys.stderr)

    # Fallback to old file cache logic
    full_path = os.path.join(CACHE_DIR, f"{protein}.json")
    pruned_name = pruned_filename(parent, protein)
    pruned_path = os.path.join(PRUNED_DIR, pruned_name)

    if not os.path.exists(full_path):
        return jsonify({"status": "needs_full", "job_id": make_prune_job_id(parent, protein)}), 200

    if is_pruned_fresh(Path(full_path), Path(pruned_path), hard_max_keep=max_keep):
        with jobs_lock:
            jobs[make_prune_job_id(parent, protein)] = {"status": "complete"}
        return jsonify({"status": "complete", "job_id": make_prune_job_id(parent, protein)}), 200

    job_id = make_prune_job_id(parent, protein)

    def _run():
        try:
            run_prune_job(
                full_json_path=Path(full_path),
                pruned_json_path=Path(pruned_path),
                parent=parent,
                current_nodes=current_nodes,
                parent_edge=parent_edge,
                hard_max_keep=max_keep,
                use_llm=False,
            )
            with jobs_lock:
                jobs[job_id] = {"status": "complete"}
        except Exception as e:
            with jobs_lock:
                jobs[job_id] = {"status": "error", "error": str(e)}

    with jobs_lock:
        jobs[job_id] = {"status": "processing", "text": "Pruning subgraph..."}
    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return jsonify({"status": "queued", "job_id": job_id}), 202


@results_bp.get('/api/expand/status/<job_id>')
def expand_status(job_id):
    """Check pruning job status."""
    try:
        parent, protein = parse_prune_job_id(job_id)
        full_path = Path(os.path.join(CACHE_DIR, f"{protein}.json"))
        pruned_path = Path(os.path.join(PRUNED_DIR, pruned_filename(parent, protein)))
        if full_path.exists() and is_pruned_fresh(full_path, pruned_path, HARD_MAX_KEEP_DEFAULT):
            return jsonify({"status": "complete"}), 200
    except (ValueError, OSError):
        pass
    with jobs_lock:
        st = jobs.get(job_id)
    if not st:
        return jsonify({"status": "unknown"}), 404
    return jsonify(st), 200


@results_bp.get('/api/expand/results/<job_id>')
def expand_results(job_id):
    """Retrieve pruned JSON results by job_id."""
    try:
        parent, protein = parse_prune_job_id(job_id)
    except (ValueError, TypeError):
        return error_response("Invalid job id", ErrorCode.INVALID_INPUT)
    fname = pruned_filename(parent, protein)
    if os.sep in fname or '/' in fname:
        return error_response("Invalid filename", ErrorCode.INVALID_INPUT, 400)
    path = os.path.join(PRUNED_DIR, fname)
    if not os.path.exists(path):
        return error_response("Not found", ErrorCode.NOT_FOUND, 404)
    return send_from_directory(PRUNED_DIR, fname)
