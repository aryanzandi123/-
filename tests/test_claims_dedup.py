"""Tests for InteractionClaim deduplication key fix."""
import sys
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import JSONB

PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------------------
# SQLite compat: compile JSONB as plain JSON
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _sqlite_jsonb_compat():
    """Register a compilation rule so JSONB columns render as JSON on SQLite."""
    from sqlalchemy.ext.compiler import compiles

    @compiles(JSONB, "sqlite")
    def _compile_jsonb_sqlite(type_, compiler, **kw):
        return "JSON"

    yield


# ---------------------------------------------------------------------------
# Override test_app: drop the partial unique index that SQLite misinterprets.
# On PostgreSQL the index has a WHERE clause (pathway_name IS NULL), but
# SQLite ignores postgresql_where and creates a plain unique index on
# (interaction_id, function_name), which blocks legitimate rows with
# different pathway_name values.
# ---------------------------------------------------------------------------

@pytest.fixture
def test_app():
    """Flask app with in-memory SQLite, partial-index workaround for claims."""
    from flask import Flask
    from models import db

    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.config["TESTING"] = True
    db.init_app(app)
    with app.app_context():
        db.create_all()
        # Drop the partial-index that SQLite cannot express correctly
        db.session.execute(text("DROP INDEX IF EXISTS claim_unique_no_pathway"))
        db.session.commit()
        yield app


def test_same_function_different_pathways_both_saved(test_app):
    """Same function_name in different pathways should create separate claims."""
    from models import Protein, Interaction, InteractionClaim, db
    from utils.db_sync import DatabaseSyncLayer

    with test_app.app_context():
        p1 = Protein(symbol="PROT_A")
        p2 = Protein(symbol="PROT_B")
        db.session.add_all([p1, p2])
        db.session.flush()

        interaction = Interaction(
            protein_a_id=p1.id,
            protein_b_id=p2.id,
            data={
                "functions": [
                    {"function": "DNA Repair", "pathway": "Base Excision Repair",
                     "arrow": "activates", "evidence": [], "pmids": []},
                    {"function": "DNA Repair", "pathway": "Nucleotide Excision Repair",
                     "arrow": "activates", "evidence": [], "pmids": []},
                ],
                "arrow": "activates",
                "direction": "a_to_b",
            },
            direction="a_to_b",
            arrow="activates",
            discovered_in_query="PROT_A",
        )
        db.session.add(interaction)
        db.session.flush()

        sync = DatabaseSyncLayer()
        count = sync._save_claims(interaction, interaction.data, "PROT_A")

        claims = InteractionClaim.query.filter_by(interaction_id=interaction.id).all()
        assert len(claims) == 2
        pathways = {c.pathway_name for c in claims}
        assert "Base Excision Repair" in pathways
        assert "Nucleotide Excision Repair" in pathways


def test_cyclic_chain_hops_create_distinct_synthetic_claims(test_app):
    """Cyclic chain like A→B→C→B revisits the canonical pair {B, C}. Both
    hops fall through to the synthetic-claim fallback (no LLM functions,
    no rehydration) and inherit the SAME parent cascade text. With the
    hop-signature disambiguator, each hop's synthetic claim gets a
    distinct function_name prefix, so both can persist on the same
    direction-agnostic Interaction row without tripping
    ``uq_claim_fn_null_pw_ctx``.
    """
    from models import Protein, Interaction, InteractionClaim, db
    from utils.db_sync import DatabaseSyncLayer

    with test_app.app_context():
        prot_b = Protein(symbol="PROT_B")
        prot_c = Protein(symbol="PROT_C")
        db.session.add_all([prot_b, prot_c])
        db.session.flush()

        # One canonical interaction row for the pair {B, C}; both
        # cycle hops (B→C and C→B) resolve to it.
        interaction = Interaction(
            protein_a_id=min(prot_b.id, prot_c.id),
            protein_b_id=max(prot_b.id, prot_c.id),
            data={},
            direction="a_to_b",
            arrow="activates",
            function_context="direct",
            discovered_in_query="PROT_A",
        )
        db.session.add(interaction)
        db.session.flush()

        cascade_summary = (
            "Parent cascade text shared by both cycle hops because "
            "neither has LLM-emitted hop-level functions."
        )

        sync = DatabaseSyncLayer()

        # Hop 1: B → C
        sync._save_claims(
            interaction,
            {
                "functions": [],
                "support_summary": cascade_summary,
                "_hop_signature": "PROT_B->PROT_C",
            },
            "PROT_A",
        )

        # Hop 2: C → B (same canonical pair, same parent cascade)
        sync._save_claims(
            interaction,
            {
                "functions": [],
                "support_summary": cascade_summary,
                "_hop_signature": "PROT_C->PROT_B",
            },
            "PROT_A",
        )

        claims = InteractionClaim.query.filter_by(interaction_id=interaction.id).all()
        assert len(claims) == 2, (
            "Expected one distinct synthetic claim per hop direction; "
            f"got {len(claims)}: {[c.function_name for c in claims]}"
        )
        names = {c.function_name for c in claims}
        assert any(n.startswith("[PROT_B->PROT_C] ") for n in names), names
        assert any(n.startswith("[PROT_C->PROT_B] ") for n in names), names


def test_direct_synthetic_claim_unchanged_without_hop_signature(test_app):
    """When no _hop_signature is present (direct interaction, not a chain
    hop), the synthetic claim's function_name is the bare summary text —
    the hop-prefix logic must not leak into non-chain code paths.
    """
    from models import Protein, Interaction, InteractionClaim, db
    from utils.db_sync import DatabaseSyncLayer

    with test_app.app_context():
        p_a = Protein(symbol="PROT_A")
        p_b = Protein(symbol="PROT_B")
        db.session.add_all([p_a, p_b])
        db.session.flush()

        interaction = Interaction(
            protein_a_id=p_a.id,
            protein_b_id=p_b.id,
            data={},
            direction="a_to_b",
            arrow="activates",
            function_context="direct",
            discovered_in_query="PROT_A",
        )
        db.session.add(interaction)
        db.session.flush()

        summary_text = "Direct binding stabilizes the complex."
        sync = DatabaseSyncLayer()
        sync._save_claims(
            interaction,
            {"functions": [], "support_summary": summary_text},
            "PROT_A",
        )

        claims = InteractionClaim.query.filter_by(interaction_id=interaction.id).all()
        assert len(claims) == 1
        assert claims[0].function_name == summary_text
