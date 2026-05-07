#!/usr/bin/env python3
"""Recompute every denormalized counter from authoritative tables.

Idempotent. Safe to run on demand or on a schedule.

Counters covered:
  - ``Protein.total_interactions`` — distinct interactions where this
    protein is ``protein_a`` or ``protein_b``.
  - ``IndirectChain.pathway_name`` — majority-vote of child claim
    pathways via ``IndirectChain.recompute_pathway_name()``.
  - ``Pathway.usage_count``, ``Pathway.hierarchy_level``,
    ``Pathway.is_leaf``, ``Pathway.ancestor_ids`` — delegated to the
    existing ``scripts/pathway_v2/step7_repairs.py`` helpers
    (already idempotent and well-tested).

Pathway.protein_count is NOT touched — no production code path
writes it and the only reader is ``services/data_builder.py:1503``
which emits it to the frontend (no JS consumer reads it). The repair
deferred a decision to a follow-up — see
``CLAUDE_DOCS/10_OPEN_ISSUES.md`` Phase D.3.

Run with:
    python3 -m scripts.repair_denormalized_counters
"""
from __future__ import annotations

import sys
from collections import defaultdict


def _recompute_protein_total_interactions(db, Protein, Interaction) -> int:
    """Recompute ``Protein.total_interactions`` from the interactions table.

    Returns the number of rows whose counter changed.
    """
    counts: dict[int, int] = defaultdict(int)
    for a_id, b_id in db.session.query(Interaction.protein_a_id, Interaction.protein_b_id).all():
        counts[a_id] += 1
        counts[b_id] += 1

    fixes = 0
    for protein in Protein.query.all():
        actual = counts.get(protein.id, 0)
        if (protein.total_interactions or 0) != actual:
            protein.total_interactions = actual
            fixes += 1
    return fixes


def _recompute_chain_pathway_names(IndirectChain) -> int:
    """Run ``recompute_pathway_name()`` on every chain.

    Returns the number of chains whose ``pathway_name`` changed.
    """
    fixes = 0
    for chain in IndirectChain.query.all():
        if chain.recompute_pathway_name():
            fixes += 1
    return fixes


def main() -> int:
    from app import app, db
    from models import Protein, Interaction, IndirectChain

    with app.app_context():
        protein_fixes = _recompute_protein_total_interactions(db, Protein, Interaction)
        chain_fixes = _recompute_chain_pathway_names(IndirectChain)

        # Pathway hierarchy + usage_count repairs — delegate to the
        # existing well-tested helpers in step7_repairs.
        from scripts.pathway_v2.step7_repairs import (
            recalculate_all_levels,
            recalculate_all_usage_counts,
            recalculate_all_is_leaf,
            recalculate_all_ancestor_ids,
        )
        from models import Pathway as _Pw, PathwayParent as _PP, PathwayInteraction as _PI
        pw_levels = recalculate_all_levels(db, _Pw, _PP)
        pw_usage = recalculate_all_usage_counts(db, _Pw, _PI)
        pw_leaf = recalculate_all_is_leaf(db, _Pw, _PP)
        pw_ancestors = recalculate_all_ancestor_ids(db, _Pw, _PP)

        db.session.commit()

        print(
            f"[REPAIR] Protein.total_interactions fixes: {protein_fixes}",
            file=sys.stderr,
        )
        print(
            f"[REPAIR] IndirectChain.pathway_name recomputed: {chain_fixes}",
            file=sys.stderr,
        )
        print(
            f"[REPAIR] Pathway.hierarchy_level fixes: {pw_levels}",
            file=sys.stderr,
        )
        print(
            f"[REPAIR] Pathway.usage_count fixes: {pw_usage}",
            file=sys.stderr,
        )
        print(
            f"[REPAIR] Pathway.is_leaf fixes: {pw_leaf}",
            file=sys.stderr,
        )
        print(
            f"[REPAIR] Pathway.ancestor_ids fixes: {pw_ancestors}",
            file=sys.stderr,
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
