"""Locks the InteractionClaim COALESCE-based unique index across the full
NULL matrix of (pathway_name, function_context).

Plain UNIQUE constraints treat NULLs as distinct, so prior constraints let
duplicate (…, NULL, NULL) rows through. The replacement index uses COALESCE
to collapse NULL → '' at the index level.
"""
import sys
from pathlib import Path

import pytest
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError

PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


@pytest.fixture(autouse=True)
def _sqlite_jsonb_compat():
    """Render JSONB columns as JSON on SQLite."""
    from sqlalchemy.ext.compiler import compiles

    @compiles(JSONB, "sqlite")
    def _compile_jsonb_sqlite(type_, compiler, **kw):
        return "JSON"

    yield


@pytest.fixture
def test_app():
    """Flask app with in-memory SQLite that honors the COALESCE unique index."""
    from flask import Flask
    from models import db

    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.config["TESTING"] = True
    db.init_app(app)
    with app.app_context():
        db.create_all()
        yield app


@pytest.fixture
def interaction_id(test_app):
    """Create a parent interaction row and return its id."""
    from models import Interaction, Protein, db

    with test_app.app_context():
        p_a = Protein(symbol="PROT_A")
        p_b = Protein(symbol="PROT_B")
        db.session.add_all([p_a, p_b])
        db.session.flush()
        inter = Interaction(
            protein_a_id=p_a.id,
            protein_b_id=p_b.id,
            data={},
            direction="a_to_b",
            arrow="activates",
            discovered_in_query="PROT_A",
        )
        db.session.add(inter)
        db.session.commit()
        return inter.id


def _insert_claim(interaction_id, function_name, pathway_name, function_context):
    from models import InteractionClaim, db

    claim = InteractionClaim(
        interaction_id=interaction_id,
        function_name=function_name,
        pathway_name=pathway_name,
        function_context=function_context,
    )
    db.session.add(claim)
    db.session.flush()
    return claim


@pytest.mark.parametrize(
    "pathway_name,function_context",
    [
        (None, None),
        ("Autophagy", None),
        (None, "direct"),
        ("Autophagy", "direct"),
    ],
    ids=["both_null", "pathway_set", "context_set", "both_set"],
)
def test_duplicate_is_blocked(test_app, interaction_id, pathway_name, function_context):
    """Second insert with same (interaction_id, function_name, pathway_name,
    function_context) must raise IntegrityError, regardless of which columns
    are NULL."""
    from models import db

    with test_app.app_context():
        _insert_claim(interaction_id, "DNA Repair", pathway_name, function_context)
        db.session.commit()

        with pytest.raises(IntegrityError):
            _insert_claim(interaction_id, "DNA Repair", pathway_name, function_context)
            db.session.commit()
        db.session.rollback()


def test_different_pathways_allowed(test_app, interaction_id):
    """Same function in different pathways are distinct claims."""
    from models import InteractionClaim, db

    with test_app.app_context():
        _insert_claim(interaction_id, "DNA Repair", "Base Excision Repair", None)
        _insert_claim(interaction_id, "DNA Repair", "Nucleotide Excision Repair", None)
        db.session.commit()

        claims = InteractionClaim.query.filter_by(interaction_id=interaction_id).all()
        assert len(claims) == 2


def test_different_contexts_allowed(test_app, interaction_id):
    """Same function in different contexts (direct vs net) are distinct claims."""
    from models import InteractionClaim, db

    with test_app.app_context():
        _insert_claim(interaction_id, "DNA Repair", "Autophagy", "direct")
        _insert_claim(interaction_id, "DNA Repair", "Autophagy", "net")
        db.session.commit()

        claims = InteractionClaim.query.filter_by(interaction_id=interaction_id).all()
        assert len(claims) == 2


def test_null_and_non_null_are_distinct(test_app, interaction_id):
    """NULL pathway_name and a concrete pathway_name are still distinct claims
    (COALESCE('', NULL) → '', and '' ≠ 'Autophagy')."""
    from models import InteractionClaim, db

    with test_app.app_context():
        _insert_claim(interaction_id, "DNA Repair", None, None)
        _insert_claim(interaction_id, "DNA Repair", "Autophagy", None)
        db.session.commit()

        claims = InteractionClaim.query.filter_by(interaction_id=interaction_id).all()
        assert len(claims) == 2


def test_different_function_names_allowed(test_app, interaction_id):
    """Different function_name, same pathway/context → two claims."""
    from models import InteractionClaim, db

    with test_app.app_context():
        _insert_claim(interaction_id, "DNA Repair", None, None)
        _insert_claim(interaction_id, "Transcription", None, None)
        db.session.commit()

        claims = InteractionClaim.query.filter_by(interaction_id=interaction_id).all()
        assert len(claims) == 2


def test_empty_string_and_null_collapse_identically(test_app, interaction_id):
    """Semantic edge: COALESCE(pathway_name, '') means NULL and '' produce the
    same index entry, so inserting both should block."""
    from models import db

    with test_app.app_context():
        _insert_claim(interaction_id, "DNA Repair", None, None)
        db.session.commit()

        with pytest.raises(IntegrityError):
            _insert_claim(interaction_id, "DNA Repair", "", None)
            db.session.commit()
        db.session.rollback()
