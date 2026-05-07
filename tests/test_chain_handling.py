"""Tests for IndirectChain model, chain claim creation, and pathway coherence."""

import sys
from pathlib import Path

import pytest
from sqlalchemy import JSON
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
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def test_app():
    """Flask app with in-memory SQLite for database integration tests."""
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
def db_app(test_app):
    """Provide the Flask test app with all tables created."""
    return test_app


@pytest.fixture
def db_session(db_app):
    """Provide a database session inside app context."""
    from models import db
    with db_app.app_context():
        yield db.session


@pytest.fixture
def sample_proteins(db_session):
    """Create ATXN3, VCP, LAMP2 proteins."""
    from models import Protein
    proteins = {}
    for sym in ("ATXN3", "VCP", "LAMP2"):
        p = Protein(symbol=sym)
        db_session.add(p)
        proteins[sym] = p
    db_session.flush()
    return proteins


@pytest.fixture
def sample_indirect_interaction(db_session, sample_proteins):
    """Create an indirect ATXN3↔LAMP2 interaction (the chain endpoint)."""
    from models import Interaction
    a = sample_proteins["ATXN3"]
    b = sample_proteins["LAMP2"]
    canon_a, canon_b = (a, b) if a.id < b.id else (b, a)
    interaction = Interaction(
        protein_a_id=canon_a.id,
        protein_b_id=canon_b.id,
        direction="a_to_b",
        data={"functions": [], "support_summary": "Indirect via VCP"},
        interaction_type="indirect",
        mediator_chain=["VCP"],
        depth=2,
        discovered_in_query="ATXN3",
    )
    db_session.add(interaction)
    db_session.flush()
    return interaction


@pytest.fixture
def sample_direct_interaction(db_session, sample_proteins):
    """Create a direct ATXN3↔VCP interaction."""
    from models import Interaction
    a = sample_proteins["ATXN3"]
    b = sample_proteins["VCP"]
    canon_a, canon_b = (a, b) if a.id < b.id else (b, a)
    interaction = Interaction(
        protein_a_id=canon_a.id,
        protein_b_id=canon_b.id,
        direction="a_to_b",
        data={"functions": [{"function": "UPS regulation", "arrow": "activates"}]},
        interaction_type="direct",
        depth=1,
        discovered_in_query="ATXN3",
    )
    db_session.add(interaction)
    db_session.flush()
    return interaction


# ---------------------------------------------------------------------------
# IndirectChain Model Tests
# ---------------------------------------------------------------------------

class TestIndirectChainModel:
    def test_create_chain(self, db_session, sample_proteins, sample_indirect_interaction):
        """IndirectChain can be created and linked to an interaction."""
        from models import IndirectChain
        chain = IndirectChain(
            chain_proteins=["ATXN3", "VCP", "LAMP2"],
            origin_interaction_id=sample_indirect_interaction.id,
            pathway_name="Autophagy",
            discovered_in_query="ATXN3",
        )
        db_session.add(chain)
        db_session.flush()

        assert chain.id is not None
        assert chain.chain_proteins == ["ATXN3", "VCP", "LAMP2"]
        assert chain.pathway_name == "Autophagy"

    def test_chain_unique_constraint(self, db_session, sample_proteins, sample_indirect_interaction):
        """Only one chain per origin interaction."""
        from models import IndirectChain
        chain1 = IndirectChain(
            chain_proteins=["ATXN3", "VCP", "LAMP2"],
            origin_interaction_id=sample_indirect_interaction.id,
            discovered_in_query="ATXN3",
        )
        db_session.add(chain1)
        db_session.flush()

        chain2 = IndirectChain(
            chain_proteins=["ATXN3", "VCP", "LAMP2"],
            origin_interaction_id=sample_indirect_interaction.id,
            discovered_in_query="ATXN3",
        )
        db_session.add(chain2)
        with pytest.raises(Exception):  # IntegrityError
            db_session.flush()
        db_session.rollback()

    def test_chain_with_arrows(self, db_session, sample_proteins, sample_indirect_interaction):
        """Chain stores typed arrow segments."""
        from models import IndirectChain
        arrows = [
            {"from": "ATXN3", "to": "VCP", "arrow": "regulates"},
            {"from": "VCP", "to": "LAMP2", "arrow": "activates"},
        ]
        chain = IndirectChain(
            chain_proteins=["ATXN3", "VCP", "LAMP2"],
            origin_interaction_id=sample_indirect_interaction.id,
            chain_with_arrows=arrows,
            discovered_in_query="ATXN3",
        )
        db_session.add(chain)
        db_session.flush()

        assert chain.chain_with_arrows[0]["arrow"] == "regulates"
        assert chain.chain_with_arrows[1]["arrow"] == "activates"


# ---------------------------------------------------------------------------
# Chain Claim Tests
# ---------------------------------------------------------------------------

class TestChainClaims:
    def test_claim_with_chain_id(self, db_session, sample_proteins, sample_indirect_interaction):
        """InteractionClaim can be linked to an IndirectChain."""
        from models import IndirectChain, InteractionClaim
        chain = IndirectChain(
            chain_proteins=["ATXN3", "VCP", "LAMP2"],
            origin_interaction_id=sample_indirect_interaction.id,
            discovered_in_query="ATXN3",
        )
        db_session.add(chain)
        db_session.flush()

        claim = InteractionClaim(
            interaction_id=sample_indirect_interaction.id,
            function_name="Autophagosome formation via VCP-mediated LAMP2 recruitment",
            arrow="activates",
            chain_id=chain.id,
            function_context="chain_derived",
        )
        db_session.add(claim)
        db_session.flush()

        assert claim.chain_id == chain.id
        assert claim.chain.id == chain.id

    def test_chain_claims_query(self, db_session, sample_proteins, sample_indirect_interaction):
        """Can query all claims for a chain."""
        from models import IndirectChain, InteractionClaim
        chain = IndirectChain(
            chain_proteins=["ATXN3", "VCP", "LAMP2"],
            origin_interaction_id=sample_indirect_interaction.id,
            discovered_in_query="ATXN3",
        )
        db_session.add(chain)
        db_session.flush()

        # Add two claims
        for name in ("Claim A", "Claim B"):
            db_session.add(InteractionClaim(
                interaction_id=sample_indirect_interaction.id,
                function_name=name,
                arrow="activates",
                chain_id=chain.id,
            ))
        db_session.flush()

        chain_claims = InteractionClaim.query.filter_by(chain_id=chain.id).all()
        assert len(chain_claims) == 2

    def test_null_chain_id_backward_compat(self, db_session, sample_proteins, sample_direct_interaction):
        """Existing claims without chain_id continue to work."""
        from models import InteractionClaim
        claim = InteractionClaim(
            interaction_id=sample_direct_interaction.id,
            function_name="Direct UPS regulation",
            arrow="activates",
        )
        db_session.add(claim)
        db_session.flush()

        assert claim.chain_id is None


# ---------------------------------------------------------------------------
# Pathway Coherence Tests
# ---------------------------------------------------------------------------

class TestPathwayCoherence:
    def test_chain_claims_share_pathway(self, db_session, sample_proteins, sample_indirect_interaction, sample_direct_interaction):
        """All claims tagged with same chain_id should share a pathway."""
        from models import IndirectChain, InteractionClaim

        chain = IndirectChain(
            chain_proteins=["ATXN3", "VCP", "LAMP2"],
            origin_interaction_id=sample_indirect_interaction.id,
            pathway_name="Autophagy",
            discovered_in_query="ATXN3",
        )
        db_session.add(chain)
        db_session.flush()

        # Indirect claim
        c1 = InteractionClaim(
            interaction_id=sample_indirect_interaction.id,
            function_name="Chain indirect claim",
            arrow="activates",
            chain_id=chain.id,
            pathway_name="Autophagy",
        )
        # Direct link claim (on ATXN3-VCP)
        c2 = InteractionClaim(
            interaction_id=sample_direct_interaction.id,
            function_name="Chain direct link claim",
            arrow="regulates",
            chain_id=chain.id,
            pathway_name="Autophagy",
        )
        db_session.add_all([c1, c2])
        db_session.flush()

        # Verify all chain claims share the same pathway
        chain_claims = InteractionClaim.query.filter_by(chain_id=chain.id).all()
        pathways = {c.pathway_name for c in chain_claims}
        assert len(pathways) == 1
        assert "Autophagy" in pathways


# ---------------------------------------------------------------------------
# Schema Extension Tests
# ---------------------------------------------------------------------------

class TestSchemaExtension:
    def test_interactor_schema_has_chain_fields(self):
        """Pipeline types schema includes chain_link_functions and _chain_pathway."""
        from pipeline.types import _INTERACTOR_OBJECT
        props = _INTERACTOR_OBJECT["properties"]
        assert "chain_link_functions" in props
        assert "_chain_pathway" in props

    def test_chain_link_functions_schema_structure(self):
        """chain_link_functions schema is an object with array values."""
        from pipeline.types import _INTERACTOR_OBJECT
        clf = _INTERACTOR_OBJECT["properties"]["chain_link_functions"]
        assert clf["type"] == "object"
        assert "additionalProperties" in clf
        assert clf["additionalProperties"]["type"] == "array"


# ---------------------------------------------------------------------------
# Context Builder Tests
# ---------------------------------------------------------------------------

class TestContextBuilderChainInfo:
    def test_chain_pathway_passed_through(self):
        """_chain_pathway is included when include_chain_info=True."""
        from pipeline.context_builders import _slim_interactor_for_function_step

        inter = {
            "primary": "LAMP2",
            "interaction_type": "indirect",
            "upstream_interactor": "VCP",
            "mediator_chain": ["VCP"],
            "depth": 2,
            "_chain_pathway": "Autophagy",
            "chain_link_functions": {"ATXN3->VCP": [{"function": "test"}]},
        }
        slim = _slim_interactor_for_function_step(inter, include_chain_info=True)

        assert slim.get("_chain_pathway") == "Autophagy"
        assert "_existing_chain_link_keys" in slim
        assert "ATXN3->VCP" in slim["_existing_chain_link_keys"]

    def test_chain_pathway_excluded_without_flag(self):
        """_chain_pathway is NOT included when include_chain_info=False."""
        from pipeline.context_builders import _slim_interactor_for_function_step

        inter = {
            "primary": "LAMP2",
            "interaction_type": "indirect",
            "_chain_pathway": "Autophagy",
        }
        slim = _slim_interactor_for_function_step(inter, include_chain_info=False)

        assert "_chain_pathway" not in slim


# ---------------------------------------------------------------------------
# Pipeline Config Tests
# ---------------------------------------------------------------------------

class TestPipelineConfigChainSteps:
    def test_modern_pipeline_includes_chain_steps(self):
        """Modern pipeline exposes the orchestrated chain resolution stages."""
        from pipeline.config_dynamic import generate_modern_pipeline
        steps = generate_modern_pipeline()
        step_names = [s.name for s in steps]
        assert "step2ab_chain_determination" in step_names
        assert "step2ax_claim_generation_explicit" in step_names
        assert "step2az_claim_generation_hidden" in step_names
        assert "step2ab2_hidden_indirect_detection" not in step_names
        assert "step2ab3_hidden_chain_determination" not in step_names
        assert "step2ab5_extract_pairs_explicit" not in step_names
        # Combined step removed — chain steps come after step2a
        assert "step2b_deep_functions_combined" not in step_names
        step2a_idx = step_names.index("step2a_functions_r1")
        assert step_names.index("step2ab_chain_determination") > step2a_idx
        assert step_names.index("step2az_claim_generation_hidden") > step2a_idx

    def test_iterative_pipeline_includes_chain_steps(self):
        """Iterative pipeline includes all 6 chain resolution steps."""
        from pipeline.config_dynamic import generate_iterative_pipeline
        steps = generate_iterative_pipeline()
        step_names = [s.name for s in steps]
        assert "step2ab_chain_determination" in step_names
        assert "step2ax_claim_generation_explicit" in step_names
        assert "step2az_claim_generation_hidden" in step_names
        assert "step2b_deep_functions_combined" not in step_names

    def test_standard_pipeline_still_has_chain_steps(self):
        """Standard pipeline includes all 6 chain resolution steps."""
        from pipeline.config_dynamic import generate_pipeline
        steps = generate_pipeline()
        step_names = [s.name for s in steps]
        assert "step2ab_chain_determination" in step_names
        assert "step2ax_claim_generation_explicit" in step_names
        assert "step2az_claim_generation_hidden" in step_names
