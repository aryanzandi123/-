"""Regression tests for non-query chain-link payload reconstruction."""

import pytest
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from unittest.mock import patch


@pytest.fixture(autouse=True)
def _sqlite_jsonb_compat():
    @compiles(JSONB, "sqlite")
    def _compile_jsonb_sqlite(type_, compiler, **kw):
        return "JSON"

    yield


def test_synthetic_chain_link_functions_are_in_pathway_payload(test_app):
    """Card view needs pathway-local chain rows, not empty same-pair stubs."""

    from models import (
        IndirectChain,
        Interaction,
        Pathway,
        PathwayInteraction,
        Protein,
        db,
    )
    from services.data_builder import build_full_json_from_db

    with test_app.app_context():
        query = Protein(symbol="QUERY")
        mediator = Protein(symbol="MED")
        target = Protein(symbol="TGT")
        pathway = Pathway(name="RNA Splicing", hierarchy_level=0, is_leaf=True)
        db.session.add_all([query, mediator, target, pathway])
        db.session.flush()

        parent = Interaction(
            protein_a_id=query.id,
            protein_b_id=target.id,
            interaction_type="indirect",
            direction="a_to_b",
            arrow="activates",
            mediator_chain=["MED"],
            depth=2,
            data={
                "step3_finalized_pathway": "RNA Splicing",
                "chain_link_functions": {
                    "MED->TGT": [
                        {
                            "function": "Splice-site rescue",
                            "arrow": "activates",
                            "cellular_process": "MED promotes TGT-linked spliceosome assembly.",
                            "pathway": "RNA Splicing",
                        }
                    ]
                },
            },
        )
        db.session.add(parent)
        db.session.flush()

        chain = IndirectChain(
            chain_proteins=["QUERY", "MED", "TGT"],
            origin_interaction_id=parent.id,
            pathway_name="RNA Splicing",
            pathway_id=pathway.id,
            discovered_in_query="QUERY",
        )
        db.session.add(chain)
        db.session.flush()
        parent.chain_id = chain.id

        db.session.add(PathwayInteraction(pathway_id=pathway.id, interaction_id=parent.id))
        db.session.commit()

        with patch("services.data_builder._inject_cross_protein_chain_claims"):
            result = build_full_json_from_db("QUERY")["snapshot_json"]
        pathway_payload = next(p for p in result["pathways"] if p["name"] == "RNA Splicing")

        assert "MED" in pathway_payload["interactor_ids"]
        assert "TGT" in pathway_payload["interactor_ids"]

        hop = next(
            ix for ix in pathway_payload["interactions"]
            if ix.get("source") == "MED" and ix.get("target") == "TGT"
        )
        assert hop["_is_chain_link"] is True
        assert hop["functions"][0]["function"] == "Splice-site rescue"


def test_interaction_instance_dedupe_collapses_embedded_duplicate_lists():
    """Merged display rows should not repeat identical claims or chain banners."""
    from services.data_builder import _dedupe_interaction_instances

    claim = {
        "id": 1,
        "function_name": "Condensate co-aggregation",
        "pathway_name": "Integrated Stress Response (ISR)",
        "chain_id": 2621,
        "locus": "chain_hop_claim",
    }
    chain = {
        "chain_id": 2621,
        "pathway_name": "Integrated Stress Response (ISR)",
        "chain_proteins": ["EIF2AK3", "EWSR1", "TDP43"],
        "role": "hop",
        "chain_pathways": ["Integrated Stress Response (ISR)"],
    }
    same_claim_from_other_source = dict(claim)
    same_claim_from_other_source["source_payload"] = "pathway-local"
    same_chain_from_other_source = dict(chain)
    same_chain_from_other_source["discovered_in_query"] = "TDP43"

    rows = _dedupe_interaction_instances([
        {
            "_db_id": 14606,
            "source": "EWSR1",
            "target": "TDP43",
            "chain_id": 2621,
            "hop_index": 1,
            "locus": "chain_hop_claim",
            "_is_chain_link": True,
            "claims": [claim, same_claim_from_other_source],
            "all_chains": [chain, same_chain_from_other_source],
        }
    ])

    assert len(rows) == 1
    assert rows[0]["claims"] == [claim]
    assert rows[0]["all_chains"] == [chain]


def test_interaction_payload_uses_db_function_context_when_json_lacks_field(test_app):
    """Old JSON blobs may lack function_context; the DB column is canonical."""

    from models import Interaction, Protein, db
    from services.data_builder import build_full_json_from_db

    with test_app.app_context():
        query = Protein(symbol="QUERY")
        target = Protein(symbol="TGT")
        db.session.add_all([query, target])
        db.session.flush()

        interaction = Interaction(
            protein_a_id=query.id,
            protein_b_id=target.id,
            interaction_type="indirect",
            direction="a_to_b",
            arrow="activates",
            depth=2,
            function_context="net",
            data={
                "functions": [
                    {
                        "function": "Net cascade output",
                        "arrow": "activates",
                        "cellular_process": "QUERY signals through a mediator to activate TGT.",
                    }
                ]
            },
        )
        db.session.add(interaction)
        db.session.commit()

        with patch("services.data_builder._inject_cross_protein_chain_claims"):
            result = build_full_json_from_db("QUERY")["snapshot_json"]

        emitted = next(ix for ix in result["interactions"] if ix.get("_db_id") == interaction.id)
        assert emitted["function_context"] == "net"


def test_net_effect_rows_are_not_reclassified_as_direct(test_app):
    """Net-effect rows keep their DB locus even when prose mentions direct assays."""

    from models import (
        ChainParticipant,
        IndirectChain,
        Interaction,
        Pathway,
        PathwayInteraction,
        Protein,
        db,
    )
    from services.data_builder import build_full_json_from_db

    with test_app.app_context():
        query = Protein(symbol="TDP43")
        mediator = Protein(symbol="GLE1")
        target = Protein(symbol="DDX3X")
        pathway = Pathway(name="Stress Granule Dynamics", hierarchy_level=0, is_leaf=True)
        db.session.add_all([query, mediator, target, pathway])
        db.session.flush()

        parent = Interaction(
            protein_a_id=query.id,
            protein_b_id=target.id,
            interaction_type="indirect",
            function_context="net",
            direction="a_to_b",
            arrow="activates",
            mediator_chain=["GLE1"],
            depth=2,
            data={
                "step3_finalized_pathway": "Stress Granule Dynamics",
                "functions": [
                    {
                        "function": "Indirect DDX3X activation",
                        "arrow": "activates",
                        "cellular_process": (
                            "TDP43 co-IP evidence is discussed, but the DDX3X "
                            "effect is mediated through GLE1."
                        ),
                        "pathway": "Stress Granule Dynamics",
                    }
                ],
            },
        )
        db.session.add(parent)
        db.session.flush()

        chain = IndirectChain(
            chain_proteins=["TDP43", "GLE1", "DDX3X"],
            origin_interaction_id=parent.id,
            pathway_name="Stress Granule Dynamics",
            pathway_id=pathway.id,
            discovered_in_query="TDP43",
        )
        db.session.add(chain)
        db.session.flush()
        parent.chain_id = chain.id
        db.session.add(ChainParticipant(chain_id=chain.id, interaction_id=parent.id, role="net_effect"))
        db.session.add(PathwayInteraction(pathway_id=pathway.id, interaction_id=parent.id))
        db.session.commit()

        with patch("services.data_builder._inject_cross_protein_chain_claims"):
            result = build_full_json_from_db("TDP43")["snapshot_json"]

        emitted = next(ix for ix in result["interactions"] if ix.get("_db_id") == parent.id)
        assert emitted["type"] == "indirect"
        assert emitted["interaction_type"] == "indirect"
        assert emitted["function_context"] == "net"
        assert emitted["locus"] == "net_effect_claim"
        assert emitted["is_net_effect"] is True
        assert emitted["source"] == "TDP43"
        assert emitted["target"] == "DDX3X"
        assert emitted["via"] == ["GLE1"]
        assert emitted["mediators"] == ["GLE1"]
        assert emitted["chain_members"] == ["TDP43", "GLE1", "DDX3X"]
        assert emitted["chain_context_pathway"] == "Stress Granule Dynamics"


def test_cross_query_injected_rows_receive_contract_fields(test_app):
    """Pathway-injected cross-query rows should match the stamped SNAP contract."""

    from models import (
        Interaction,
        InteractionClaim,
        Pathway,
        PathwayInteraction,
        Protein,
        db,
    )
    from services.data_builder import build_full_json_from_db

    with test_app.app_context():
        query = Protein(symbol="TDP43")
        anchor = Protein(symbol="DDX3X")
        cross_a = Protein(symbol="SETX")
        cross_b = Protein(symbol="C9ORF72")
        pathway = Pathway(name="Stress Granule Dynamics", hierarchy_level=0, is_leaf=True)
        db.session.add_all([query, anchor, cross_a, cross_b, pathway])
        db.session.flush()

        anchor_interaction = Interaction(
            protein_a_id=query.id,
            protein_b_id=anchor.id,
            interaction_type="direct",
            function_context="direct",
            direction="a_to_b",
            arrow="binds",
            depth=1,
            data={"functions": [], "step3_finalized_pathway": "Stress Granule Dynamics"},
        )
        cross_interaction = Interaction(
            protein_a_id=cross_a.id,
            protein_b_id=cross_b.id,
            interaction_type="direct",
            function_context="direct",
            direction="a_to_b",
            arrow="binds",
            depth=1,
            data={"functions": [], "step3_finalized_pathway": "Stress Granule Dynamics"},
        )
        db.session.add_all([anchor_interaction, cross_interaction])
        db.session.flush()
        db.session.add(PathwayInteraction(pathway_id=pathway.id, interaction_id=anchor_interaction.id))
        db.session.add(InteractionClaim(
            interaction_id=cross_interaction.id,
            function_name="R-loop stress coupling",
            arrow="regulates",
            direction="main_to_primary",
            mechanism="SETX and C9ORF72 are co-mentioned in stress granule biology.",
            pathway_name="Stress Granule Dynamics",
            pathway_id=pathway.id,
            function_context="direct",
        ))
        db.session.commit()

        with patch("services.data_builder._inject_cross_protein_chain_claims"):
            result = build_full_json_from_db("TDP43")["snapshot_json"]

        snap_entry = next(
            ix for ix in result["interactions"]
            if ix.get("_db_id") == cross_interaction.id and ix.get("_cross_query")
        )
        pathway_payload = next(p for p in result["pathways"] if p["name"] == "Stress Granule Dynamics")
        pathway_entry = next(
            ix for ix in pathway_payload["cross_query_interactions"]
            if ix.get("source") == "SETX" and ix.get("target") == "C9ORF72"
        )

        for emitted in (snap_entry, pathway_entry):
            assert emitted["locus"] == "direct_claim"
            assert emitted["is_net_effect"] is False
            assert emitted["source"] == "SETX"
            assert emitted["target"] == "C9ORF72"

        assert snap_entry["claims"][0]["locus"] == "direct_claim"
        assert snap_entry["claims"][0]["source"] == "SETX"
        assert snap_entry["claims"][0]["target"] == "C9ORF72"


def test_cross_query_net_rows_are_not_left_direct_net(test_app):
    """A late-injected function_context=net row should emit net-effect semantics."""

    from models import (
        Interaction,
        InteractionClaim,
        Pathway,
        PathwayInteraction,
        Protein,
        db,
    )
    from services.data_builder import build_full_json_from_db

    with test_app.app_context():
        query = Protein(symbol="TDP43")
        anchor = Protein(symbol="DDX3X")
        cross_a = Protein(symbol="SETX")
        cross_b = Protein(symbol="EIF4G1")
        pathway = Pathway(name="RNA Metabolism", hierarchy_level=0, is_leaf=True)
        db.session.add_all([query, anchor, cross_a, cross_b, pathway])
        db.session.flush()

        anchor_interaction = Interaction(
            protein_a_id=query.id,
            protein_b_id=anchor.id,
            interaction_type="direct",
            function_context="direct",
            direction="a_to_b",
            arrow="binds",
            depth=1,
            data={"functions": [], "step3_finalized_pathway": "RNA Metabolism"},
        )
        cross_interaction = Interaction(
            protein_a_id=cross_a.id,
            protein_b_id=cross_b.id,
            interaction_type="direct",
            function_context="net",
            direction="a_to_b",
            arrow="activates",
            depth=2,
            data={"functions": [], "step3_finalized_pathway": "RNA Metabolism"},
        )
        db.session.add_all([anchor_interaction, cross_interaction])
        db.session.flush()
        db.session.add(PathwayInteraction(pathway_id=pathway.id, interaction_id=anchor_interaction.id))
        db.session.add(InteractionClaim(
            interaction_id=cross_interaction.id,
            function_name="Net translational output",
            arrow="activates",
            direction="main_to_primary",
            mechanism="SETX produces a downstream EIF4G1 effect through an unresolved chain.",
            pathway_name="RNA Metabolism",
            pathway_id=pathway.id,
            function_context="net",
        ))
        db.session.commit()

        with patch("services.data_builder._inject_cross_protein_chain_claims"):
            result = build_full_json_from_db("TDP43")["snapshot_json"]

        snap_entry = next(
            ix for ix in result["interactions"]
            if ix.get("_db_id") == cross_interaction.id and ix.get("_cross_query")
        )
        pathway_payload = next(p for p in result["pathways"] if p["name"] == "RNA Metabolism")
        pathway_entry = next(
            ix for ix in pathway_payload["cross_query_interactions"]
            if ix.get("source") == "SETX" and ix.get("target") == "EIF4G1"
        )

        for emitted in (snap_entry, pathway_entry):
            assert emitted["type"] == "indirect"
            assert emitted["interaction_type"] == "indirect"
            assert emitted["function_context"] == "net"
            assert emitted["locus"] == "net_effect_claim"
            assert emitted["is_net_effect"] is True

        assert snap_entry["claims"][0]["locus"] == "net_effect_claim"
        assert snap_entry["claims"][0]["source"] == "SETX"
        assert snap_entry["claims"][0]["target"] == "EIF4G1"


def test_cross_query_chain_hop_claims_receive_hop_contract(test_app):
    """Late-injected chain-hop rows should carry hop-local claim metadata."""

    from models import (
        ChainParticipant,
        IndirectChain,
        Interaction,
        InteractionClaim,
        Pathway,
        PathwayInteraction,
        Protein,
        db,
    )
    from services.data_builder import build_full_json_from_db

    with test_app.app_context():
        query = Protein(symbol="TDP43")
        anchor = Protein(symbol="DDX3X")
        upstream = Protein(symbol="SETX")
        mediator = Protein(symbol="C9ORF72")
        target = Protein(symbol="EIF4G1")
        pathway = Pathway(name="Translation Initiation", hierarchy_level=0, is_leaf=True)
        db.session.add_all([query, anchor, upstream, mediator, target, pathway])
        db.session.flush()

        anchor_interaction = Interaction(
            protein_a_id=query.id,
            protein_b_id=anchor.id,
            interaction_type="direct",
            function_context="direct",
            direction="a_to_b",
            arrow="binds",
            depth=1,
            data={"functions": [], "step3_finalized_pathway": "Translation Initiation"},
        )
        cross_parent = Interaction(
            protein_a_id=upstream.id,
            protein_b_id=target.id,
            interaction_type="indirect",
            function_context="net",
            direction="a_to_b",
            arrow="activates",
            depth=2,
            mediator_chain=["C9ORF72"],
            data={"functions": [], "step3_finalized_pathway": "Translation Initiation"},
        )
        hop = Interaction(
            protein_a_id=mediator.id,
            protein_b_id=target.id,
            interaction_type="direct",
            function_context="direct",
            direction="a_to_b",
            arrow="activates",
            depth=1,
            data={"functions": [], "step3_finalized_pathway": "Translation Initiation"},
        )
        db.session.add_all([anchor_interaction, cross_parent, hop])
        db.session.flush()

        chain = IndirectChain(
            chain_proteins=["SETX", "C9ORF72", "EIF4G1"],
            origin_interaction_id=cross_parent.id,
            pathway_name="Translation Initiation",
            pathway_id=pathway.id,
            discovered_in_query="SETX",
        )
        db.session.add(chain)
        db.session.flush()
        cross_parent.chain_id = chain.id
        hop.chain_id = chain.id
        db.session.add_all([
            PathwayInteraction(pathway_id=pathway.id, interaction_id=anchor_interaction.id),
            ChainParticipant(chain_id=chain.id, interaction_id=cross_parent.id, role="net_effect"),
            ChainParticipant(chain_id=chain.id, interaction_id=hop.id, role="hop"),
            InteractionClaim(
                interaction_id=hop.id,
                function_name="EIF4G1 initiation control",
                arrow="activates",
                direction="main_to_primary",
                mechanism="C9ORF72 supports EIF4G1 translation initiation.",
                pathway_name="Translation Initiation",
                pathway_id=pathway.id,
                function_context="direct",
                chain_id=chain.id,
            ),
        ])
        db.session.commit()

        with patch("services.data_builder._inject_cross_protein_chain_claims"):
            result = build_full_json_from_db("TDP43")["snapshot_json"]

        snap_entry = next(
            ix for ix in result["interactions"]
            if ix.get("_db_id") == hop.id and ix.get("_cross_query")
        )

        assert snap_entry["_is_chain_link"] is True
        assert snap_entry["locus"] == "chain_hop_claim"
        assert snap_entry["chain_id"] == chain.id
        assert snap_entry["hop_index"] == 1
        assert snap_entry["chain_members"] == ["SETX", "C9ORF72", "EIF4G1"]
        assert snap_entry["claims"][0]["locus"] == "chain_hop_claim"
        assert snap_entry["claims"][0]["chain_id"] == chain.id
        assert snap_entry["claims"][0]["source"] == "C9ORF72"
        assert snap_entry["claims"][0]["target"] == "EIF4G1"
        assert snap_entry["claims"][0]["hop_index"] == 1


def test_reconstruct_chain_links_prefers_real_db_terminal_hop(test_app):
    """Terminal hops should use their DB row, not stale parent chain JSONB."""

    from models import (
        ChainParticipant,
        IndirectChain,
        Interaction,
        InteractionClaim,
        Pathway,
        PathwayInteraction,
        Protein,
        db,
    )
    from services.data_builder import build_full_json_from_db

    with test_app.app_context():
        query = Protein(symbol="TDP43")
        mediator = Protein(symbol="GLE1")
        target = Protein(symbol="DDX3X")
        pathway = Pathway(name="Stress Granule Dynamics", hierarchy_level=0, is_leaf=True)
        stale_pathway = Pathway(name="RNA Metabolism & Translation Control", hierarchy_level=0, is_leaf=True)
        db.session.add_all([query, mediator, target, pathway, stale_pathway])
        db.session.flush()

        parent = Interaction(
            protein_a_id=query.id,
            protein_b_id=target.id,
            interaction_type="indirect",
            function_context="net",
            direction="a_to_b",
            arrow="activates",
            mediator_chain=["GLE1"],
            depth=2,
            data={
                "step3_finalized_pathway": "Stress Granule Dynamics",
                "chain_link_functions": {
                    "GLE1->DDX3X": [
                        {
                            "function": "Stale parent-pathway copy",
                            "arrow": "activates",
                            "cellular_process": "GLE1 regulates DDX3X in stale JSONB.",
                            "pathway": "RNA Metabolism & Translation Control",
                        }
                    ]
                },
            },
        )
        hop = Interaction(
            protein_a_id=mediator.id,
            protein_b_id=target.id,
            interaction_type="direct",
            function_context="direct",
            direction="a_to_b",
            arrow="activates",
            depth=1,
            data={
                "step3_finalized_pathway": "Stress Granule Dynamics",
                "functions": [
                    {
                        "function": "DDX3X translation control",
                        "arrow": "activates",
                        "cellular_process": "GLE1 supports DDX3X activity.",
                        "pathway": "Stress Granule Dynamics",
                    }
                ],
            },
        )
        db.session.add_all([parent, hop])
        db.session.flush()

        chain = IndirectChain(
            chain_proteins=["TDP43", "GLE1", "DDX3X"],
            origin_interaction_id=parent.id,
            pathway_name="Stress Granule Dynamics",
            pathway_id=pathway.id,
            discovered_in_query="TDP43",
        )
        db.session.add(chain)
        db.session.flush()
        parent.chain_id = chain.id
        hop.chain_id = chain.id
        db.session.add_all([
            ChainParticipant(chain_id=chain.id, interaction_id=parent.id, role="net_effect"),
            ChainParticipant(chain_id=chain.id, interaction_id=hop.id, role="hop"),
            PathwayInteraction(pathway_id=pathway.id, interaction_id=parent.id),
            PathwayInteraction(pathway_id=pathway.id, interaction_id=hop.id),
            InteractionClaim(
                interaction_id=hop.id,
                function_name="DDX3X translation control",
                arrow="activates",
                direction="main_to_primary",
                mechanism="GLE1 supports DDX3X activity.",
                pathway_name="Stress Granule Dynamics",
                pathway_id=pathway.id,
                function_context="direct",
                chain_id=chain.id,
            ),
        ])
        db.session.commit()

        with patch("services.data_builder._inject_cross_protein_chain_claims"):
            result = build_full_json_from_db("TDP43")["snapshot_json"]

        hop_payload = next(
            ix for ix in result["interactions"]
            if ix.get("source") == "GLE1" and ix.get("target") == "DDX3X" and ix.get("_is_chain_link")
        )
        assert hop_payload["_db_id"] == hop.id
        assert hop_payload["locus"] == "chain_hop_claim"
        assert hop_payload["chain_id"] == chain.id
        assert hop_payload["hop_index"] == 1
        assert hop_payload["chain_members"] == ["TDP43", "GLE1", "DDX3X"]
        assert hop_payload["chain_context_pathway"] == "Stress Granule Dynamics"
        assert hop_payload["hop_local_pathway"] == "Stress Granule Dynamics"
        assert hop_payload["functions"][0]["pathway"] == "Stress Granule Dynamics"
        assert hop_payload["functions"][0]["function"] == "DDX3X translation control"
        assert hop_payload["claims"][0]["chain_id"] == chain.id
        assert hop_payload["claims"][0]["locus"] == "chain_hop_claim"

        pathway_payload = next(p for p in result["pathways"] if p["name"] == "Stress Granule Dynamics")
        pathway_hop = next(
            ix for ix in pathway_payload["interactions"]
            if ix.get("_db_id") == hop.id and ix.get("_is_chain_link")
        )
        assert pathway_hop["step3_finalized_pathway"] == "Stress Granule Dynamics"
        assert pathway_hop["claims"][0]["function_name"] == "DDX3X translation control"
        assert pathway_hop["claims"][0]["chain_id"] == chain.id
        assert pathway_hop["claims"][0]["locus"] == "chain_hop_claim"


def test_reconstructed_chain_link_uses_hop_membership_for_identity(test_app):
    """Same hop DB row should not duplicate under the parent chain role."""

    from models import (
        ChainParticipant,
        IndirectChain,
        Interaction,
        InteractionClaim,
        Pathway,
        PathwayInteraction,
        Protein,
        db,
    )
    from services.data_builder import build_full_json_from_db

    with test_app.app_context():
        query = Protein(symbol="TDP43")
        mediator = Protein(symbol="EWSR1")
        target = Protein(symbol="FUS")
        pathway = Pathway(name="RNA Granule Assembly", hierarchy_level=0, is_leaf=True)
        db.session.add_all([query, mediator, target, pathway])
        db.session.flush()

        parent = Interaction(
            protein_a_id=query.id,
            protein_b_id=target.id,
            interaction_type="indirect",
            function_context="net",
            direction="a_to_b",
            arrow="activates",
            mediator_chain=["EWSR1"],
            depth=2,
            data={
                "step3_finalized_pathway": "RNA Granule Assembly",
                "chain_link_functions": {
                    "TDP43->EWSR1": [
                        {
                            "function": "Parent-carried hop copy",
                            "arrow": "activates",
                            "cellular_process": "TDP43 engages EWSR1 in RNA granules.",
                            "pathway": "RNA Granule Assembly",
                        }
                    ]
                },
            },
        )
        hop = Interaction(
            protein_a_id=query.id,
            protein_b_id=mediator.id,
            interaction_type="direct",
            function_context="direct",
            direction="a_to_b",
            arrow="activates",
            depth=1,
            data={
                "step3_finalized_pathway": "RNA Granule Assembly",
                "functions": [
                    {
                        "function": "Hop-local assembly",
                        "arrow": "activates",
                        "cellular_process": "TDP43 directly recruits EWSR1.",
                        "pathway": "RNA Granule Assembly",
                    }
                ],
            },
        )
        db.session.add_all([parent, hop])
        db.session.flush()

        chain = IndirectChain(
            chain_proteins=["TDP43", "EWSR1", "FUS"],
            origin_interaction_id=parent.id,
            pathway_name="RNA Granule Assembly",
            pathway_id=pathway.id,
            discovered_in_query="TDP43",
        )
        db.session.add(chain)
        db.session.flush()
        parent.chain_id = chain.id
        hop.chain_id = chain.id
        db.session.add_all([
            ChainParticipant(chain_id=chain.id, interaction_id=parent.id, role="origin"),
            ChainParticipant(chain_id=chain.id, interaction_id=hop.id, role="hop"),
            PathwayInteraction(pathway_id=pathway.id, interaction_id=parent.id),
            PathwayInteraction(pathway_id=pathway.id, interaction_id=hop.id),
            InteractionClaim(
                interaction_id=hop.id,
                function_name="Hop-local assembly",
                arrow="activates",
                direction="main_to_primary",
                mechanism="TDP43 directly recruits EWSR1.",
                pathway_name="RNA Granule Assembly",
                pathway_id=pathway.id,
                function_context="direct",
                chain_id=chain.id,
            ),
        ])
        db.session.commit()

        with patch("services.data_builder._inject_cross_protein_chain_claims"):
            result = build_full_json_from_db("TDP43")["snapshot_json"]

        hop_rows = [
            ix for ix in result["interactions"]
            if ix.get("_db_id") == hop.id and {ix.get("source"), ix.get("target")} == {"TDP43", "EWSR1"}
        ]

        assert len(hop_rows) == 1
        assert hop_rows[0]["chain_id"] == chain.id
        assert hop_rows[0]["hop_index"] == 0
        assert hop_rows[0]["locus"] == "chain_hop_claim"
        assert hop_rows[0]["all_chains"][0]["role"] == "hop"
        assert "|role:hop" in hop_rows[0]["_interaction_instance_id"]


def test_chain_hop_identity_prefers_hop_local_pathway_over_stale_direct_pathway():
    """A chain-hop row's display identity follows its scoped hop pathway."""

    from services.data_builder import _interaction_instance_id_for

    identity = _interaction_instance_id_for({
        "_db_id": 14612,
        "source": "CYLD",
        "target": "TDP43",
        "chain_id": 2626,
        "hop_index": 0,
        "locus": "chain_hop_claim",
        "step3_finalized_pathway": "Protein Quality Control",
        "hop_local_pathway": "Inflammatory Signaling",
        "chain_context_pathway": "Inflammatory Signaling",
        "all_chains": [{"chain_id": 2626, "role": "hop"}],
    })

    assert "pathway:inflammatory signaling" in identity
    assert "pathway:protein quality control" not in identity


def test_multi_chain_hop_rows_scope_claims_to_visible_chain(test_app):
    """A shared hop must not carry evidence from a different chain instance."""

    from models import (
        ChainParticipant,
        IndirectChain,
        Interaction,
        InteractionClaim,
        Pathway,
        PathwayInteraction,
        Protein,
        db,
    )
    from services.data_builder import build_full_json_from_db

    with test_app.app_context():
        query = Protein(symbol="QUERY")
        mediator = Protein(symbol="MED")
        target_a = Protein(symbol="TGTA")
        target_b = Protein(symbol="TGTB")
        pathway_a = Pathway(name="Pathway A", hierarchy_level=0, is_leaf=True)
        pathway_b = Pathway(name="Pathway B", hierarchy_level=0, is_leaf=True)
        db.session.add_all([query, mediator, target_a, target_b, pathway_a, pathway_b])
        db.session.flush()

        parent_a = Interaction(
            protein_a_id=query.id,
            protein_b_id=target_a.id,
            interaction_type="indirect",
            function_context="net",
            direction="a_to_b",
            arrow="activates",
            mediator_chain=["MED"],
            depth=2,
            data={
                "step3_finalized_pathway": "Pathway A",
                "chain_link_functions": {
                    "QUERY->MED": [{
                        "function": "MED relay A",
                        "arrow": "activates",
                        "cellular_process": "MED starts the A-specific relay.",
                        "pathway": "Pathway A",
                    }]
                },
            },
        )
        parent_b = Interaction(
            protein_a_id=query.id,
            protein_b_id=target_b.id,
            interaction_type="indirect",
            function_context="net",
            direction="a_to_b",
            arrow="activates",
            mediator_chain=["MED"],
            depth=2,
            data={
                "step3_finalized_pathway": "Pathway B",
                "chain_link_functions": {
                    "QUERY->MED": [{
                        "function": "MED relay B",
                        "arrow": "activates",
                        "cellular_process": "MED starts the B-specific relay.",
                        "pathway": "Pathway B",
                    }]
                },
            },
        )
        hop = Interaction(
            protein_a_id=query.id,
            protein_b_id=mediator.id,
            interaction_type="direct",
            function_context="direct",
            direction="a_to_b",
            arrow="activates",
            depth=1,
            data={"functions": []},
        )
        db.session.add_all([parent_a, parent_b, hop])
        db.session.flush()

        chain_a = IndirectChain(
            chain_proteins=["QUERY", "MED", "TGTA"],
            origin_interaction_id=parent_a.id,
            pathway_name="Pathway A",
            pathway_id=pathway_a.id,
            discovered_in_query="QUERY",
        )
        chain_b = IndirectChain(
            chain_proteins=["QUERY", "MED", "TGTB"],
            origin_interaction_id=parent_b.id,
            pathway_name="Pathway B",
            pathway_id=pathway_b.id,
            discovered_in_query="QUERY",
        )
        db.session.add_all([chain_a, chain_b])
        db.session.flush()
        parent_a.chain_id = chain_a.id
        parent_b.chain_id = chain_b.id
        hop.chain_id = chain_a.id

        db.session.add_all([
            ChainParticipant(chain_id=chain_a.id, interaction_id=parent_a.id, role="net_effect"),
            ChainParticipant(chain_id=chain_b.id, interaction_id=parent_b.id, role="net_effect"),
            ChainParticipant(chain_id=chain_a.id, interaction_id=hop.id, role="hop"),
            ChainParticipant(chain_id=chain_b.id, interaction_id=hop.id, role="hop"),
            PathwayInteraction(pathway_id=pathway_a.id, interaction_id=parent_a.id),
            PathwayInteraction(pathway_id=pathway_b.id, interaction_id=parent_b.id),
            InteractionClaim(
                interaction_id=hop.id,
                function_name="A-specific hop claim",
                arrow="activates",
                direction="main_to_primary",
                mechanism="MED carries the A-specific signal.",
                evidence=[{"paper_title": "A paper"}],
                pathway_name="Pathway A",
                pathway_id=pathway_a.id,
                function_context="direct",
                chain_id=chain_a.id,
            ),
            InteractionClaim(
                interaction_id=hop.id,
                function_name="B-specific hop claim",
                arrow="activates",
                direction="main_to_primary",
                mechanism="MED carries the B-specific signal.",
                evidence=[{"paper_title": "B paper"}],
                pathway_name="Pathway B",
                pathway_id=pathway_b.id,
                function_context="direct",
                chain_id=chain_b.id,
            ),
        ])
        db.session.commit()

        with patch("services.data_builder._inject_cross_protein_chain_claims"):
            result = build_full_json_from_db("QUERY")["snapshot_json"]

        hop_rows = [
            ix for ix in result["interactions"]
            if ix.get("_db_id") == hop.id and ix.get("locus") == "chain_hop_claim"
        ]
        assert len(hop_rows) >= 2
        for ix in hop_rows:
            row_scope = {ix["chain_id"]}
            claim_chain_ids = {claim.get("chain_id") for claim in ix.get("claims", [])}
            assert claim_chain_ids <= row_scope
            summary_chain_ids = {chain.get("chain_id") for chain in ix.get("all_chains", [])}
            assert summary_chain_ids <= row_scope

            matching_chain = next(
                (
                    chain for chain in ix.get("all_chains", [])
                    if chain.get("chain_id") == ix.get("chain_id")
                ),
                None,
            )
            if matching_chain:
                assert ix["chain_members"] == matching_chain["chain_proteins"]

        chain_specific_rows = [ix for ix in hop_rows if ix.get("_parent_chain")]
        assert chain_specific_rows
        for ix in chain_specific_rows:
            row_scope = {ix["chain_id"]}
            claim_chain_ids = {claim.get("chain_id") for claim in ix.get("claims", [])}
            assert claim_chain_ids <= row_scope

        row_a = next(ix for ix in chain_specific_rows if ix["chain_id"] == chain_a.id)
        row_b = next(ix for ix in chain_specific_rows if ix["chain_id"] == chain_b.id)
        assert [claim["function_name"] for claim in row_a["claims"]] == ["A-specific hop claim"]
        assert [claim["function_name"] for claim in row_b["claims"]] == ["B-specific hop claim"]
        assert row_a["_interaction_instance_id"] != row_b["_interaction_instance_id"]
        assert "chain:" in row_a["_interaction_instance_id"]
        assert "hop:" in row_a["_interaction_instance_id"]
        assert "locus:chain_hop_claim" in row_a["_interaction_instance_id"]


def test_chain_hop_instance_dedup_collapses_direction_variants():
    """Same chain hop identity should collapse opposite source/target variants."""

    from services.data_builder import _dedupe_interaction_instances

    first = {
        "_db_id": 14606,
        "source": "TDP43",
        "target": "EWSR1",
        "chain_id": 2621,
        "hop_index": 1,
        "locus": "chain_hop_claim",
        "_is_chain_link": True,
    }
    duplicate_reverse = {
        "_db_id": 14606,
        "source": "EWSR1",
        "target": "TDP43",
        "chain_id": 2621,
        "_chain_position": 1,
        "locus": "chain_hop_claim",
        "_is_chain_link": True,
    }

    rows = _dedupe_interaction_instances([first, duplicate_reverse])

    assert rows == [first]
    assert first["_interaction_instance_id"].startswith("db:14606|pair:ews")
    assert "|chain:2621|hop:1|locus:chain_hop_claim|" in first["_interaction_instance_id"]


def test_reconstruct_chain_links_matches_mixed_case_chain_symbols(test_app):
    """Chain text casing should not hide an existing DB-backed hop row."""

    from models import (
        ChainParticipant,
        IndirectChain,
        Interaction,
        Pathway,
        PathwayInteraction,
        Protein,
        db,
    )
    from services.data_builder import build_full_json_from_db

    with test_app.app_context():
        query = Protein(symbol="TDP43")
        mediator = Protein(symbol="C9ORF72")
        target = Protein(symbol="EIF4G1")
        pathway = Pathway(name="Translation Initiation", hierarchy_level=0, is_leaf=True)
        stale_pathway = Pathway(name="Stale Parent Pathway", hierarchy_level=0, is_leaf=True)
        db.session.add_all([query, mediator, target, pathway, stale_pathway])
        db.session.flush()

        parent = Interaction(
            protein_a_id=query.id,
            protein_b_id=target.id,
            interaction_type="indirect",
            function_context="net",
            direction="a_to_b",
            arrow="activates",
            mediator_chain=["C9orf72"],
            depth=2,
            data={
                "step3_finalized_pathway": "Translation Initiation",
                "chain_link_functions": {
                    "C9orf72->EIF4G1": [
                        {
                            "function": "Stale mixed-case parent copy",
                            "arrow": "activates",
                            "cellular_process": "C9orf72 regulates EIF4G1 in stale JSONB.",
                            "pathway": "Stale Parent Pathway",
                        }
                    ]
                },
            },
        )
        hop = Interaction(
            protein_a_id=mediator.id,
            protein_b_id=target.id,
            interaction_type="direct",
            function_context="direct",
            direction="a_to_b",
            arrow="activates",
            depth=1,
            data={
                "step3_finalized_pathway": "Translation Initiation",
                "functions": [
                    {
                        "function": "EIF4G1 initiation control",
                        "arrow": "activates",
                        "cellular_process": "C9ORF72 supports EIF4G1 translation initiation.",
                        "pathway": "Translation Initiation",
                    }
                ],
            },
        )
        db.session.add_all([parent, hop])
        db.session.flush()

        chain = IndirectChain(
            chain_proteins=["TDP43", "C9orf72", "EIF4G1"],
            origin_interaction_id=parent.id,
            pathway_name="Translation Initiation",
            pathway_id=pathway.id,
            discovered_in_query="TDP43",
        )
        db.session.add(chain)
        db.session.flush()
        parent.chain_id = chain.id
        hop.chain_id = chain.id
        db.session.add_all([
            ChainParticipant(chain_id=chain.id, interaction_id=parent.id, role="net_effect"),
            ChainParticipant(chain_id=chain.id, interaction_id=hop.id, role="hop"),
            PathwayInteraction(pathway_id=pathway.id, interaction_id=parent.id),
            PathwayInteraction(pathway_id=pathway.id, interaction_id=hop.id),
        ])
        db.session.commit()

        with patch("services.data_builder._inject_cross_protein_chain_claims"):
            result = build_full_json_from_db("TDP43")["snapshot_json"]

        hop_payload = next(
            ix for ix in result["interactions"]
            if ix.get("source") == "C9orf72" and ix.get("target") == "EIF4G1" and ix.get("_is_chain_link")
        )
        assert hop_payload["_db_id"] == hop.id
        assert hop_payload["locus"] == "chain_hop_claim"
        assert hop_payload["hop_index"] == 1
        assert hop_payload["functions"][0]["function"] == "EIF4G1 initiation control"
        assert hop_payload["functions"][0]["pathway"] == "Translation Initiation"


def test_read_side_does_not_render_generic_chain_nodes(test_app):
    """Old saved chains with generic entities should not become card nodes."""

    from models import (
        IndirectChain,
        Interaction,
        Pathway,
        PathwayInteraction,
        Protein,
        db,
    )
    from services.data_builder import build_full_json_from_db

    with test_app.app_context():
        query = Protein(symbol="TDP43")
        optn = Protein(symbol="OPTN")
        tbk1 = Protein(symbol="TBK1")
        pathway = Pathway(name="Protein Degradation", hierarchy_level=0, is_leaf=True)
        db.session.add_all([query, optn, tbk1, pathway])
        db.session.flush()

        parent = Interaction(
            protein_a_id=optn.id,
            protein_b_id=query.id,
            interaction_type="indirect",
            direction="a_to_b",
            arrow="activates",
            mediator_chain=["TBK1", "Ubiquitin"],
            depth=3,
            data={
                "step3_finalized_pathway": "Protein Degradation",
                "chain_link_functions": {
                    "TBK1->OPTN": [
                        {
                            "function": "OPTN phosphorylation",
                            "arrow": "activates",
                            "cellular_process": "TBK1 phosphorylates OPTN.",
                            "pathway": "Protein Degradation",
                        }
                    ]
                },
            },
        )
        db.session.add(parent)
        db.session.flush()

        chain = IndirectChain(
            chain_proteins=["TBK1", "OPTN", "Ubiquitin", "TDP43"],
            origin_interaction_id=parent.id,
            pathway_name="Protein Degradation",
            pathway_id=pathway.id,
            discovered_in_query="TDP43",
        )
        db.session.add(chain)
        db.session.flush()
        parent.chain_id = chain.id
        db.session.add(PathwayInteraction(pathway_id=pathway.id, interaction_id=parent.id))
        db.session.commit()

        with patch("services.data_builder._inject_cross_protein_chain_claims"):
            result = build_full_json_from_db("TDP43")["snapshot_json"]

        assert "Ubiquitin" not in result["proteins"]
        pathway_payload = next(p for p in result["pathways"] if p["name"] == "Protein Degradation")
        assert "Ubiquitin" not in pathway_payload["interactor_ids"]

        hop = next(
            ix for ix in pathway_payload["interactions"]
            if ix.get("source") == "TBK1" and ix.get("target") == "OPTN"
        )
        assert hop["functions"][0]["function"] == "OPTN phosphorylation"
        assert hop["_chain_entity"]["chain_proteins"] == ["TBK1", "OPTN"]


def test_chain_entity_prefers_canonical_chain_arrows(test_app):
    """Reader should use IndirectChain arrows, not stale interaction JSON."""

    from models import (
        IndirectChain,
        Interaction,
        Pathway,
        PathwayInteraction,
        Protein,
        db,
    )
    from services.data_builder import build_full_json_from_db

    with test_app.app_context():
        query = Protein(symbol="QUERY")
        mediator = Protein(symbol="MED")
        target = Protein(symbol="TGT")
        pathway = Pathway(name="Mitochondrial Biology", hierarchy_level=0, is_leaf=True)
        db.session.add_all([query, mediator, target, pathway])
        db.session.flush()

        parent = Interaction(
            protein_a_id=query.id,
            protein_b_id=target.id,
            interaction_type="indirect",
            direction="a_to_b",
            arrow="binds",
            mediator_chain=["MED"],
            depth=2,
            data={
                "step3_finalized_pathway": "Mitochondrial Biology",
                "chain_with_arrows": [
                    {"from": "QUERY", "to": "MED", "arrow": "inhibits"},
                    {"from": "MED", "to": "TGT", "arrow": "inhibits"},
                ],
                "functions": [
                    {
                        "function": "Complex formation",
                        "arrow": "complex",
                        "direction": "b_to_a",
                        "cellular_process": "QUERY forms a complex with TGT.",
                        "pathway": "Mitochondrial Biology",
                    }
                ],
            },
        )
        db.session.add(parent)
        db.session.flush()

        chain = IndirectChain(
            chain_proteins=["QUERY", "MED", "TGT"],
            chain_with_arrows=[
                {"from": "QUERY", "to": "MED", "arrow": "complex"},
                {"from": "MED", "to": "TGT", "arrow": "complex"},
            ],
            origin_interaction_id=parent.id,
            pathway_name="Mitochondrial Biology",
            pathway_id=pathway.id,
            discovered_in_query="QUERY",
        )
        db.session.add(chain)
        db.session.flush()
        parent.chain_id = chain.id
        db.session.add(PathwayInteraction(pathway_id=pathway.id, interaction_id=parent.id))
        db.session.commit()

        with patch("services.data_builder._inject_cross_protein_chain_claims"):
            result = build_full_json_from_db("QUERY")["snapshot_json"]

        emitted = next(ix for ix in result["interactions"] if ix.get("_db_id") == parent.id)
        assert emitted["arrow"] == "binds"
        assert emitted["functions"][0]["arrow"] == "binds"
        assert emitted["functions"][0]["direction"] == "primary_to_main"
        assert emitted["_chain_entity"]["chain_with_arrows"] == [
            {"from": "QUERY", "to": "MED", "arrow": "binds"},
            {"from": "MED", "to": "TGT", "arrow": "binds"},
        ]
