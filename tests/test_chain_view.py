"""Tests for the ChainView derivation layer (#4) and the chain_id
canonicalization (#6) refactor.

ChainView is the single source of truth for chain state. Every legacy
field (mediator_chain, upstream_interactor, depth) and every query-
relative neighbor view (upstream_of_query, downstream_of_query, etc.)
is derived from one canonical full_chain list. This test file pins
that contract: any future change that breaks the derivation rules
will fail loudly.
"""
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.chain_view import ChainView, chain_view_from_interaction


# ---------------------------------------------------------------------------
# Pure derivation tests (no DB)
# ---------------------------------------------------------------------------


def test_empty_chain_view():
    cv = ChainView.empty()
    assert cv.is_empty
    assert cv.chain_length == 0
    assert cv.depth == 0
    assert cv.mediator_chain == []
    assert cv.upstream_interactor is None
    assert cv.upstream_of_query() == []
    assert cv.downstream_of_query() == []


def test_query_at_head_3chain():
    cv = ChainView.from_full_chain(["TDP43", "HNRNPA1", "GRN"], query_protein="TDP43")
    assert cv.full_chain == ["TDP43", "HNRNPA1", "GRN"]
    assert cv.query_position == 0
    assert cv.chain_length == 3
    assert cv.depth == 2
    assert cv.mediator_chain == ["HNRNPA1"]
    assert cv.upstream_interactor == "HNRNPA1"
    assert cv.upstream_of_query() == []
    assert cv.downstream_of_query() == ["HNRNPA1", "GRN"]
    assert cv.immediate_upstream_of_query() is None
    assert cv.immediate_downstream_of_query() == "HNRNPA1"


def test_query_in_middle_3chain():
    cv = ChainView.from_full_chain(["VCP", "TDP43", "GRN"], query_protein="TDP43")
    assert cv.query_position == 1
    assert cv.chain_length == 3
    assert cv.depth == 2
    assert cv.mediator_chain == ["TDP43"]  # legacy: non-endpoint slice
    assert cv.upstream_interactor == "TDP43"  # legacy: full_chain[-2]
    assert cv.upstream_of_query() == ["VCP"]
    assert cv.downstream_of_query() == ["GRN"]
    assert cv.immediate_upstream_of_query() == "VCP"
    assert cv.immediate_downstream_of_query() == "GRN"


def test_query_at_tail_3chain():
    cv = ChainView.from_full_chain(["VCP", "HSP70", "TDP43"], query_protein="TDP43")
    assert cv.query_position == 2
    assert cv.chain_length == 3
    assert cv.depth == 2
    assert cv.mediator_chain == ["HSP70"]
    assert cv.upstream_interactor == "HSP70"
    assert cv.upstream_of_query() == ["VCP", "HSP70"]
    assert cv.downstream_of_query() == []
    assert cv.immediate_upstream_of_query() == "HSP70"
    assert cv.immediate_downstream_of_query() is None


def test_query_in_middle_5chain():
    cv = ChainView.from_full_chain(
        ["A", "B", "TDP43", "D", "E"], query_protein="TDP43"
    )
    assert cv.query_position == 2
    assert cv.chain_length == 5
    assert cv.depth == 4
    assert cv.mediator_chain == ["B", "TDP43", "D"]
    assert cv.upstream_interactor == "D"  # full_chain[-2]
    assert cv.upstream_of_query() == ["A", "B"]
    assert cv.downstream_of_query() == ["D", "E"]
    assert cv.immediate_upstream_of_query() == "B"
    assert cv.immediate_downstream_of_query() == "D"


def test_query_at_position_3_in_6chain():
    cv = ChainView.from_full_chain(
        ["A", "B", "C", "TDP43", "E", "F"], query_protein="TDP43"
    )
    assert cv.query_position == 3
    assert cv.chain_length == 6
    assert cv.depth == 5
    assert cv.upstream_of_query() == ["A", "B", "C"]
    assert cv.downstream_of_query() == ["E", "F"]


def test_mediator_chain_for_2_element_chain_is_empty():
    cv = ChainView.from_full_chain(["A", "B"], query_protein="A")
    assert cv.mediator_chain == []
    assert cv.depth == 1
    assert cv.upstream_interactor == "A"  # the only non-target element


def test_strip_marker_characters_on_ingest():
    cv = ChainView.from_full_chain(["^VCP^", "**TDP43**", " GRN "], query_protein="TDP43")
    assert cv.full_chain == ["VCP", "TDP43", "GRN"]
    assert cv.query_position == 1


def test_query_position_explicit_overrides_search():
    """If a caller passes query_position explicitly, it's trusted —
    even when the protein is not findable by name (e.g. case mismatch
    that the case-insensitive search would also handle)."""
    cv = ChainView.from_full_chain(
        ["A", "B", "C"], query_protein="NotInChain", query_position=1
    )
    assert cv.query_position == 1


def test_query_position_unknown_when_query_not_in_chain():
    cv = ChainView.from_full_chain(["A", "B", "C"], query_protein="NOT_HERE")
    assert cv.query_position is None
    assert cv.upstream_of_query() == []
    assert cv.downstream_of_query() == []


def test_from_interaction_data_uses_chain_context():
    cv = ChainView.from_interaction_data({
        "chain_context": {
            "full_chain": ["A", "B", "C", "D"],
            "query_protein": "B",
            "query_position": 1,
        }
    })
    assert cv.full_chain == ["A", "B", "C", "D"]
    assert cv.query_position == 1
    assert cv.mediator_chain == ["B", "C"]


def test_from_interaction_data_does_not_reconstruct_from_mediator_chain():
    """mediator_chain + primary alone cannot encode the query's biological
    position (head / middle / tail). The old fallback force-prepended
    query_protein at position 0, silently inverting query-at-tail chains
    (e.g. AKT1 → TSC2 → RHEB → MTOR → RPTOR → ULK1 became ULK1 → ... →
    AKT1) and caused [CHAIN HOP CLAIM MISSING] mismatches between 2ax
    and db_sync. The reconstruction is gone; chain_context.full_chain
    is now the only accepted source. Callers must populate it (every
    in-memory write path uses apply_to_dict, which does).
    """
    cv = ChainView.from_interaction_data(
        {"mediator_chain": ["VCP"], "primary": "LAMP2"},
        query_protein="ATXN3",
    )
    assert cv.is_empty
    assert cv.full_chain == []


def test_from_interaction_data_returns_empty_for_garbage():
    assert ChainView.from_interaction_data(None).is_empty
    assert ChainView.from_interaction_data({}).is_empty
    assert ChainView.from_interaction_data({"mediator_chain": []}).is_empty


def test_to_chain_context_round_trip():
    original = ChainView.from_full_chain(
        ["A", "B", "C", "D"], query_protein="C", query_position=2,
    )
    rt = ChainView.from_interaction_data(
        {"chain_context": original.to_chain_context()}
    )
    assert rt.full_chain == original.full_chain
    assert rt.query_position == original.query_position
    assert rt.chain_length == original.chain_length


def test_chain_view_is_immutable():
    cv = ChainView.from_full_chain(["A", "B", "C"], query_protein="A")
    with pytest.raises(Exception):
        cv.full_chain = ["X", "Y"]  # frozen dataclass


def test_mediator_chain_returns_copy_not_reference():
    """Callers must not be able to mutate the underlying full_chain by
    mutating the returned mediator_chain list."""
    cv = ChainView.from_full_chain(["A", "B", "C", "D"], query_protein="A")
    mc = cv.mediator_chain
    mc.append("MUTATED")
    assert cv.mediator_chain == ["B", "C"]  # unchanged


# ---------------------------------------------------------------------------
# ORM-side tests (#6 — chain_id FK + linked_chain relationship)
# ---------------------------------------------------------------------------
#
# These tests boot Flask + in-memory SQLite to exercise the new
# Interaction.chain_id column and the linked_chain relationship.


@pytest.fixture
def sqlite_app():
    from flask import Flask
    from sqlalchemy.dialects.postgresql import JSONB
    from sqlalchemy.ext.compiler import compiles
    from sqlalchemy.pool import StaticPool

    @compiles(JSONB, "sqlite")
    def _compile_jsonb_sqlite(type_, compiler, **kw):
        return "JSON"

    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    # ``:memory:`` databases are per-connection by default; multiple
    # SQLAlchemy connections (e.g. fixture setup vs. test_client request
    # vs. background thread) would each get an isolated in-memory DB
    # and miss each other's writes. ``StaticPool`` with
    # ``check_same_thread=False`` keeps a single shared connection so
    # the whole test sees the same data.
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
        "connect_args": {"check_same_thread": False},
        "poolclass": StaticPool,
    }
    from models import db
    db.init_app(app)
    with app.app_context():
        db.create_all()
        yield app


def _setup_chain(proteins):
    """Helper: insert ``proteins`` and the canonical pair interactions
    plus an IndirectChain. The caller MUST already be inside an
    ``app_context`` (the SQLite in-memory DB is per-context, so
    creating a nested context here would lose the rows).

    Returns ``(interactions_by_pair, chain_row)``.
    """
    from models import db, Protein, Interaction, IndirectChain

    prots = [Protein(symbol=s) for s in proteins]
    db.session.add_all(prots)
    db.session.flush()

    interactions = {}
    for i in range(len(proteins) - 1):
        a, b = prots[i], prots[i + 1]
        ax, bx = (a, b) if a.id < b.id else (b, a)
        inter = Interaction(
            protein_a_id=ax.id, protein_b_id=bx.id,
            direction="a_to_b", arrow="activates", data={},
        )
        db.session.add(inter)
        interactions[(proteins[i], proteins[i + 1])] = inter
    first, last = prots[0], prots[-1]
    ax, bx = (first, last) if first.id < last.id else (last, first)
    net_inter = Interaction(
        protein_a_id=ax.id, protein_b_id=bx.id,
        direction="a_to_b", arrow="regulates", data={},
    )
    db.session.add(net_inter)
    interactions[(proteins[0], proteins[-1])] = net_inter
    db.session.flush()

    chain = IndirectChain(
        chain_proteins=list(proteins),
        origin_interaction_id=net_inter.id,
        discovered_in_query=proteins[0],
    )
    db.session.add(chain)
    db.session.flush()
    return interactions, chain


def test_interaction_chain_id_column_exists(sqlite_app):
    from models import Interaction
    cols = {c.name for c in Interaction.__table__.columns}
    assert "chain_id" in cols


def test_chain_view_reads_through_linked_chain(sqlite_app):
    """When ``chain_id`` is set on an interaction, ``chain_view`` should
    return the chain from the linked IndirectChain row, not from the
    JSONB column."""
    from models import db
    with sqlite_app.app_context():
        interactions, chain = _setup_chain(["A", "B", "C"])
        ab = interactions[("A", "B")]
        ab.chain_id = chain.id
        db.session.flush()

        cv = ab.chain_view
        assert cv.full_chain == ["A", "B", "C"]
        assert cv.depth == 2
        assert cv.mediator_chain == ["B"]


def test_computed_properties_match_chain_view(sqlite_app):
    from models import db
    with sqlite_app.app_context():
        interactions, chain = _setup_chain(["A", "B", "C", "D"])
        ab = interactions[("A", "B")]
        ab.chain_id = chain.id
        db.session.flush()

        cv = ab.chain_view
        assert ab.computed_mediator_chain == cv.mediator_chain
        assert ab.computed_upstream_interactor == cv.upstream_interactor
        assert ab.computed_depth == cv.depth


def test_chain_view_falls_back_to_jsonb_when_no_chain_id(sqlite_app):
    """An interaction with chain_context JSONB but no chain_id FK should
    still produce a working chain_view from the JSONB."""
    from models import db, Protein, Interaction

    with sqlite_app.app_context():
        a = Protein(symbol="X")
        b = Protein(symbol="Y")
        db.session.add_all([a, b])
        db.session.flush()
        inter = Interaction(
            protein_a_id=a.id, protein_b_id=b.id,
            direction="a_to_b", arrow="regulates",
            data={
                "chain_context": {
                    "full_chain": ["X", "MED", "Y"],
                    "query_protein": "X",
                    "query_position": 0,
                }
            },
        )
        db.session.add(inter)
        db.session.flush()

        cv = inter.chain_view
        assert cv.full_chain == ["X", "MED", "Y"]
        assert cv.depth == 2


def test_tag_claims_with_chain_sets_interaction_chain_id(sqlite_app):
    """``DatabaseSyncLayer._tag_claims_with_chain`` must set both the
    interaction's ``chain_id`` and every claim's ``chain_id`` so the
    interaction-level FK and the per-claim links stay in sync."""
    from models import db, InteractionClaim
    from utils.db_sync import DatabaseSyncLayer

    with sqlite_app.app_context():
        interactions, chain = _setup_chain(["A", "B", "C"])
        ab = interactions[("A", "B")]
        claim = InteractionClaim(interaction_id=ab.id, function_name="test_fn")
        db.session.add(claim)
        db.session.flush()

        sync = DatabaseSyncLayer()
        sync._tag_claims_with_chain(ab, chain)
        db.session.flush()

        assert ab.chain_id == chain.id
        refreshed_claim = InteractionClaim.query.filter_by(
            interaction_id=ab.id
        ).first()
        assert refreshed_claim.chain_id == chain.id


def test_one_chain_row_per_chain_not_per_interaction(sqlite_app):
    """The whole point of #6: a 4-protein chain creates ONE IndirectChain
    row, with all participating interactions linking to it via chain_id —
    not 4 separate JSONB copies of the same chain."""
    from models import db, IndirectChain
    from utils.db_sync import DatabaseSyncLayer

    with sqlite_app.app_context():
        interactions, chain = _setup_chain(["A", "B", "C", "D"])
        # 4 proteins → 3 hops + 1 net-effect = 4 interactions, 1 chain
        all_inters = list(interactions.values())
        assert len(all_inters) == 4

        sync = DatabaseSyncLayer()
        for inter in all_inters:
            sync._tag_claims_with_chain(inter, chain)
        db.session.flush()

        # Exactly one chain row
        assert IndirectChain.query.count() == 1
        # Every interaction links to it
        for inter in all_inters:
            assert inter.chain_id == chain.id
        # And reading any of them through chain_view returns the same chain
        for inter in all_inters:
            cv = inter.chain_view
            assert cv.full_chain == ["A", "B", "C", "D"]
            assert cv.depth == 3


# ---------------------------------------------------------------------------
# Single write surface — apply_to_dict / apply_to_interaction
# ---------------------------------------------------------------------------
#
# These tests pin the contract that ALL chain-related fields on an
# interactor (in-memory dict OR ORM row) come from one ChainView. The
# four legacy storage shapes (mediator_chain, upstream_interactor, depth,
# chain_context) cannot drift because they're written by a single helper
# from a single source.


def test_apply_to_dict_writes_all_legacy_fields_from_one_source():
    cv = ChainView.from_full_chain(["A", "B", "C", "D"], query_protein="A")
    d = {"primary": "D", "functions": []}
    cv.apply_to_dict(d)
    assert d["mediator_chain"] == ["B", "C"]
    assert d["upstream_interactor"] == "C"
    assert d["depth"] == 3
    assert d["chain_context"]["full_chain"] == ["A", "B", "C", "D"]
    assert d["chain_context"]["query_position"] == 0
    assert d["chain_context"]["chain_length"] == 4
    # Original fields preserved
    assert d["primary"] == "D"
    assert d["functions"] == []


def test_apply_to_dict_query_in_middle_writes_correct_chain_context():
    cv = ChainView.from_full_chain(["VCP", "TDP43", "GRN"], query_protein="TDP43")
    d = {"primary": "GRN"}
    cv.apply_to_dict(d)
    assert d["chain_context"]["query_position"] == 1
    assert d["chain_context"]["full_chain"] == ["VCP", "TDP43", "GRN"]
    # Legacy fields use the same view
    assert d["mediator_chain"] == ["TDP43"]
    assert d["depth"] == 2


def test_apply_to_dict_empty_view_scrubs_stale_fields():
    """An empty ChainView clears chain fields rather than leaving stale
    data behind. Use case: re-running Track A should reset chain
    annotations on interactors that no longer have a resolved chain."""
    d = {
        "primary": "X",
        "mediator_chain": ["STALE"],
        "upstream_interactor": "STALE",
        "depth": 99,
        "chain_context": {"full_chain": ["S", "T", "A", "L", "E"]},
    }
    ChainView.empty().apply_to_dict(d)
    assert "mediator_chain" not in d
    assert "upstream_interactor" not in d
    assert "depth" not in d
    assert "chain_context" not in d
    assert d["primary"] == "X"


def test_apply_to_interaction_sets_all_orm_fields(sqlite_app):
    """``apply_to_interaction`` sets every chain-related ORM column
    AND mirrors chain_context into ``data["chain_context"]`` so legacy
    JSONB readers stay consistent. When a ``chain_record`` is passed,
    ``chain_id`` is set too."""
    from models import db, Protein, Interaction, IndirectChain

    with sqlite_app.app_context():
        a = Protein(symbol="A")
        b = Protein(symbol="B")
        db.session.add_all([a, b])
        db.session.flush()
        inter = Interaction(
            protein_a_id=a.id, protein_b_id=b.id,
            direction="a_to_b", arrow="activates", data={},
        )
        db.session.add(inter)
        db.session.flush()

        chain = IndirectChain(
            chain_proteins=["A", "B", "C"],
            origin_interaction_id=inter.id,
            discovered_in_query="A",
        )
        db.session.add(chain)
        db.session.flush()

        cv = ChainView.from_full_chain(["A", "B", "C"], query_protein="A")
        cv.apply_to_interaction(inter, chain_record=chain)
        db.session.flush()

        # FK link
        assert inter.chain_id == chain.id
        # Legacy columns derived from the view
        assert inter.mediator_chain == ["B"]
        assert inter.upstream_interactor == "B"
        assert inter.depth == 2
        # chain_context column AND data.chain_context mirror
        assert inter.chain_context["full_chain"] == ["A", "B", "C"]
        assert inter.data["chain_context"]["full_chain"] == ["A", "B", "C"]
        assert inter.data["chain_context"]["query_position"] == 0


def test_apply_to_interaction_empty_view_scrubs_orm_fields(sqlite_app):
    """Empty view applied to a row that had chain state clears every
    chain field — used when a row stops being part of any chain."""
    from models import db, Protein, Interaction

    with sqlite_app.app_context():
        a = Protein(symbol="A")
        b = Protein(symbol="B")
        db.session.add_all([a, b])
        db.session.flush()
        inter = Interaction(
            protein_a_id=a.id, protein_b_id=b.id,
            direction="a_to_b", arrow="activates",
            mediator_chain=["STALE"],
            upstream_interactor="STALE",
            depth=99,
            chain_context={"full_chain": ["X", "Y"]},
            data={"chain_context": {"full_chain": ["X", "Y"]}, "primary": "B"},
        )
        db.session.add(inter)
        db.session.flush()

        ChainView.empty().apply_to_interaction(inter)
        db.session.flush()

        assert inter.mediator_chain is None
        assert inter.upstream_interactor is None
        assert inter.depth == 1  # default, not the stale 99
        assert inter.chain_context is None
        assert "chain_context" not in (inter.data or {})
        # Other data fields preserved
        assert inter.data.get("primary") == "B"


def test_apply_to_interaction_returns_self_for_chaining(sqlite_app):
    from models import db, Protein, Interaction
    with sqlite_app.app_context():
        a = Protein(symbol="A")
        b = Protein(symbol="B")
        db.session.add_all([a, b])
        db.session.flush()
        inter = Interaction(
            protein_a_id=a.id, protein_b_id=b.id,
            direction="a_to_b", arrow="binds", data={},
        )
        db.session.add(inter)
        db.session.flush()
        returned = ChainView.from_full_chain(
            ["A", "B"], query_protein="A"
        ).apply_to_interaction(inter)
        assert returned is inter


# ---------------------------------------------------------------------------
# /api/chain/<id> endpoint
# ---------------------------------------------------------------------------


def test_chain_endpoint_returns_canonical_chain(sqlite_app):
    """``GET /api/chain/<id>`` returns the canonical IndirectChain row
    plus its participants — frontend reads this instead of the JSONB
    chain_context blob on each individual interaction."""
    from models import db
    from utils.db_sync import DatabaseSyncLayer
    from routes.results import results_bp

    sqlite_app.register_blueprint(results_bp)
    client = sqlite_app.test_client()

    with sqlite_app.app_context():
        interactions, chain = _setup_chain(["A", "B", "C"])
        sync = DatabaseSyncLayer()
        for inter in interactions.values():
            sync._tag_claims_with_chain(inter, chain)
        # commit so the test_client request (which opens its own session)
        # actually sees the data instead of an uncommitted transaction.
        db.session.commit()
        chain_id = chain.id

    resp = client.get(f"/api/chain/{chain_id}")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["chain_id"] == chain_id
    assert body["chain_proteins"] == ["A", "B", "C"]
    assert body["chain_length"] == 3
    assert body["discovered_in_query"] == "A"
    # Participants list contains the 3 interactions we tagged
    participant_pairs = {(p["protein_a"], p["protein_b"]) for p in body["participants"]}
    assert ("A", "B") in participant_pairs
    assert ("B", "C") in participant_pairs
    assert ("A", "C") in participant_pairs


def test_chain_endpoint_404s_for_unknown_id(sqlite_app):
    from routes.results import results_bp
    sqlite_app.register_blueprint(results_bp)
    client = sqlite_app.test_client()
    resp = client.get("/api/chain/99999")
    assert resp.status_code == 404
