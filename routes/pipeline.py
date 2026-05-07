"""Pipeline blueprint: /api/queries, /api/pipeline/*, /api/repair-pathways."""

import logging
import sys
import threading
import traceback

from flask import Blueprint, request, jsonify, current_app

from models import db
from services.state import PIPELINE_STATUS, PIPELINE_LOCK
from services.error_helpers import error_response, ErrorCode

pipeline_bp = Blueprint('pipeline', __name__)


def run_pipeline_task(app, mode, start_step, query_filter=None):
    """Background task to run the unified pathway pipeline."""
    from scripts.pathway_v2.run_pipeline import run_pathway_pipeline

    with app.app_context():
        try:
            with PIPELINE_LOCK:
                PIPELINE_STATUS["is_running"] = True
                PIPELINE_STATUS["error"] = None
                PIPELINE_STATUS["logs"] = []
                PIPELINE_STATUS["query_filter"] = query_filter
                query_info = f" for query '{query_filter}'" if query_filter else ""
                PIPELINE_STATUS["logs"].append(f"Starting pipeline (Mode: {mode}, Start: {start_step}){query_info}...")

            # P5.1: when query_filter is set, scope the pipeline to
            # that protein's interactions instead of the whole DB.
            # Previously the parameters were collected, the user was
            # told "running for query X", but run_pathway_pipeline()
            # was called with no arguments — a global re-run.
            scoped_kwargs = {}
            if query_filter:
                from models import Interaction
                _ids = [
                    i.id for i in
                    Interaction.query.filter_by(discovered_in_query=query_filter).all()
                ]
                if _ids:
                    scoped_kwargs["interaction_ids"] = _ids
                    scoped_kwargs["quick_assign"] = True

            result = run_pathway_pipeline(**scoped_kwargs)

            with PIPELINE_LOCK:
                PIPELINE_STATUS["is_running"] = False
                PIPELINE_STATUS["current_step"] = "Complete"
                PIPELINE_STATUS["query_filter"] = None
                status = "passed" if result.get("passed") else "failed"
                PIPELINE_STATUS["logs"].append(
                    f"Pipeline {status} in {result.get('total_seconds', 0):.1f}s "
                    f"({len(result.get('steps_completed', []))} steps)."
                )

        except Exception as e:
            logging.error(f"Pipeline failed: {e}")
            with PIPELINE_LOCK:
                PIPELINE_STATUS["is_running"] = False
                PIPELINE_STATUS["error"] = str(e)
                PIPELINE_STATUS["query_filter"] = None
                PIPELINE_STATUS["logs"].append(f"Error: {str(e)}")


@pipeline_bp.route('/api/queries', methods=['GET'])
def get_queries():
    """Return list of unique queries with interaction counts."""
    from models import Interaction
    from sqlalchemy import func

    results = db.session.query(
        Interaction.discovered_in_query,
        func.count(Interaction.id).label('count')
    ).filter(
        Interaction.discovered_in_query.isnot(None)
    ).group_by(
        Interaction.discovered_in_query
    ).order_by(
        func.count(Interaction.id).desc()
    ).all()

    return jsonify({
        "queries": [
            {"name": q, "interaction_count": c}
            for q, c in results
        ]
    })


@pipeline_bp.route('/api/pipeline/run', methods=['POST'])
def run_pipeline():
    """Start the V2 pipeline."""
    data = request.json or {}
    mode = data.get('mode', 'full')
    query_filter = data.get('query', None)
    try:
        start_step = int(data.get('step', 1))
    except (ValueError, TypeError):
        start_step = 1

    with PIPELINE_LOCK:
        if PIPELINE_STATUS["is_running"]:
            return error_response("Pipeline is already running", ErrorCode.PIPELINE_BUSY, 409)

    thread = threading.Thread(
        target=run_pipeline_task,
        args=(current_app._get_current_object(), mode, start_step, query_filter)
    )
    thread.daemon = True
    thread.start()

    return jsonify({
        "message": "Pipeline started",
        "mode": mode,
        "step": start_step,
        "query": query_filter or "all"
    })


@pipeline_bp.route('/api/pipeline/clear', methods=['POST'])
def clear_pathway_data():
    """Clear pathway tables for fresh rebuild."""
    from models import Pathway, PathwayParent, PathwayInteraction, Interaction

    data = request.json or {}
    keep_assignments = data.get('keep_assignments', False)
    dry_run = data.get('dry_run', False)

    with PIPELINE_LOCK:
        if PIPELINE_STATUS["is_running"]:
            return error_response("Cannot clear while pipeline is running", ErrorCode.PIPELINE_BUSY, 409)

    try:
        parents_count = db.session.query(PathwayParent).count()
        pi_count = db.session.query(PathwayInteraction).count()
        pathways_count = db.session.query(Pathway).count()

        interactions_with_data = db.session.query(Interaction).filter(
            db.or_(
                Interaction.data.has_key('step2_proposal'),
                Interaction.data.has_key('step3_finalized_pathway')
            )
        ).count()

        if dry_run:
            return jsonify({
                "dry_run": True,
                "would_delete": {
                    "pathway_parents": parents_count,
                    "pathway_interactions": pi_count,
                    "pathways": pathways_count,
                    "interactions_with_pathway_data": interactions_with_data if not keep_assignments else 0
                }
            })

        deleted_parents = db.session.query(PathwayParent).delete()
        deleted_pi = db.session.query(PathwayInteraction).delete()
        deleted_pathways = db.session.query(Pathway).delete()

        cleared_interactions = 0
        if not keep_assignments:
            interactions = db.session.query(Interaction).filter(
                db.or_(
                    Interaction.data.has_key('step2_proposal'),
                    Interaction.data.has_key('step3_finalized_pathway')
                )
            ).all()

            for ix in interactions:
                if ix.data:
                    new_data = {k: v for k, v in ix.data.items()
                               if k not in ['step2_proposal', 'step3_finalized_pathway']}
                    ix.data = new_data
                    cleared_interactions += 1

        db.session.commit()

        return jsonify({
            "success": True,
            "deleted": {
                "pathway_parents": deleted_parents,
                "pathway_interactions": deleted_pi,
                "pathways": deleted_pathways,
                "interactions_cleared": cleared_interactions
            },
            "message": "Pathway data cleared. Run pipeline to rebuild."
        })

    except Exception as e:
        db.session.rollback()
        return error_response("Failed to clear pathway data", ErrorCode.INTERNAL, 500)


@pipeline_bp.route('/api/pipeline/status', methods=['GET'])
def get_pipeline_status():
    """Get current pipeline status."""
    with PIPELINE_LOCK:
        return jsonify(PIPELINE_STATUS)


@pipeline_bp.route('/api/repair-pathways/<protein>', methods=['POST'])
def repair_pathways(protein):
    """Re-run pathway assignment for an existing protein's interactions."""
    try:
        from models import Protein, Interaction, PathwayInteraction

        data = request.json or {}
        clear_existing = data.get('clear_existing', False)
        skip_hierarchy = data.get('skip_hierarchy', False)

        protein_obj = Protein.query.filter_by(symbol=protein.upper()).first()
        if not protein_obj:
            return error_response(
                f"Protein '{protein}' not found in database",
                ErrorCode.NOT_FOUND,
                404,
            )

        interactions = Interaction.query.filter(
            (Interaction.protein_a_id == protein_obj.id) |
            (Interaction.protein_b_id == protein_obj.id)
        ).all()

        if not interactions:
            return error_response(
                f"No interactions found for protein '{protein}'",
                ErrorCode.NOT_FOUND,
                404,
            )

        if clear_existing:
            for ix in interactions:
                if ix.data:
                    d = dict(ix.data)
                    d.pop('step2_proposal', None)
                    d.pop('step3_finalized_pathway', None)
                    ix.data = d
            db.session.commit()

            for ix in interactions:
                PathwayInteraction.query.filter_by(interaction_id=ix.id).delete()
            db.session.commit()

        from scripts.pathway_v2.run_pipeline import run_pathway_pipeline

        interaction_ids = [i.id for i in interactions]

        before_step2 = sum(1 for i in interactions if i.data and 'step2_proposal' in i.data)
        before_step3 = sum(1 for i in interactions if i.data and 'step3_finalized_pathway' in i.data)
        before_links = PathwayInteraction.query.filter(
            PathwayInteraction.interaction_id.in_(interaction_ids)
        ).count()

        # P5.1: pass interaction_ids and quick_assign=True so the
        # repair is SCOPED to this protein's interactions and uses the
        # fast/main mode. Previously this called run_pathway_pipeline()
        # with no scope — re-assigning every protein's pathways across
        # the whole DB just because the user asked to repair one.
        result = run_pathway_pipeline(
            quick_assign=True,
            interaction_ids=interaction_ids,
            skip_hierarchy=skip_hierarchy,
        )
        verification_result = result.get("verification", {})

        db.session.expire_all()
        interactions = Interaction.query.filter(
            (Interaction.protein_a_id == protein_obj.id) |
            (Interaction.protein_b_id == protein_obj.id)
        ).all()

        after_step2 = sum(1 for i in interactions if i.data and 'step2_proposal' in i.data)
        after_step3 = sum(1 for i in interactions if i.data and 'step3_finalized_pathway' in i.data)
        after_links = PathwayInteraction.query.filter(
            PathwayInteraction.interaction_id.in_([i.id for i in interactions])
        ).count()

        return jsonify({
            "success": True,
            "protein": protein.upper(),
            "total_interactions": len(interactions),
            "before": {
                "step2_assigned": before_step2,
                "step3_assigned": before_step3,
                "pathway_links": before_links
            },
            "after": {
                "step2_assigned": after_step2,
                "step3_assigned": after_step3,
                "pathway_links": after_links
            },
            "new_assignments": {
                "step2": after_step2 - before_step2,
                "step3": after_step3 - before_step3,
                "pathway_links": after_links - before_links
            },
            "verification": verification_result
        })

    except ImportError as e:
        print(f"[ERROR] Pipeline import failed: {e}", file=sys.stderr)
        return error_response("Required pipeline modules not available", ErrorCode.INTERNAL, 500)
    except Exception as e:
        traceback.print_exc()
        return error_response("Pathway repair failed", ErrorCode.INTERNAL, 500)


@pipeline_bp.route('/api/backfill-claims', methods=['POST'])
def backfill_claims():
    """Backfill missing InteractionClaim rows for all interactions."""
    try:
        from utils.db_sync import backfill_missing_claims
        result = backfill_missing_claims()
        return jsonify({"success": True, **result})
    except Exception as e:
        traceback.print_exc()
        return error_response("Claim backfill failed", ErrorCode.INTERNAL, 500)
