#!/usr/bin/env python3
"""Tests for utils/protein_database.py -- file-based protein interaction database."""

import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import utils.protein_database as pdb


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Redirect PROTEINS_DIR and CACHE_DIR to a temp directory."""
    proteins_dir = tmp_path / "proteins"
    proteins_dir.mkdir()
    monkeypatch.setattr(pdb, "PROTEINS_DIR", proteins_dir)
    monkeypatch.setattr(pdb, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(pdb, "OLD_CACHE_DIR", tmp_path)
    return tmp_path


@pytest.fixture
def sample_interaction():
    """A minimal interaction dict."""
    return {
        "primary": "VCP",
        "confidence": 0.9,
        "arrow": "binds",
        "direction": "main_to_primary",
    }


# ---------------------------------------------------------------------------
# save_interaction tests
# ---------------------------------------------------------------------------


class TestSaveInteraction:
    def test_creates_symmetric_files(self, sample_interaction):
        """Both A->B and B->A interaction files are created."""
        result = pdb.save_interaction("ATXN3", "VCP", sample_interaction)
        assert result is True
        assert (pdb.PROTEINS_DIR / "ATXN3" / "interactions" / "VCP.json").exists()
        assert (pdb.PROTEINS_DIR / "VCP" / "interactions" / "ATXN3.json").exists()

    def test_enriches_metadata(self, sample_interaction):
        """Saved data includes protein_a, protein_b, and timestamps."""
        pdb.save_interaction("ATXN3", "VCP", sample_interaction)
        path = pdb.PROTEINS_DIR / "ATXN3" / "interactions" / "VCP.json"
        data = json.loads(path.read_text())
        assert data["protein_a"] == "ATXN3"
        assert data["protein_b"] == "VCP"
        assert "first_discovered" in data
        assert "last_updated" in data

    def test_symmetric_copy_flips_direction(self, sample_interaction):
        """Symmetric copy has swapped protein_a/protein_b and flipped direction."""
        pdb.save_interaction("ATXN3", "VCP", sample_interaction)
        path = pdb.PROTEINS_DIR / "VCP" / "interactions" / "ATXN3.json"
        data = json.loads(path.read_text())
        assert data["protein_a"] == "VCP"
        assert data["protein_b"] == "ATXN3"
        assert data["direction"] == "primary_to_main"


# ---------------------------------------------------------------------------
# get_all_interactions tests
# ---------------------------------------------------------------------------


class TestGetAllInteractions:
    def test_returns_empty_for_unknown_protein(self):
        """Unknown protein returns empty list."""
        result = pdb.get_all_interactions("UNKNOWN")
        assert result == []

    def test_returns_direct_interactions(self, sample_interaction):
        """Direct interactions are found."""
        pdb.save_interaction("ATXN3", "VCP", sample_interaction)
        interactions = pdb.get_all_interactions("ATXN3")
        partners = {i.get("primary") for i in interactions}
        assert "VCP" in partners

    def test_returns_reverse_interactions(self, sample_interaction):
        """Reverse/symmetric interactions are found."""
        pdb.save_interaction("ATXN3", "VCP", sample_interaction)
        interactions = pdb.get_all_interactions("VCP")
        partners = {i.get("primary") for i in interactions}
        assert "ATXN3" in partners

    def test_no_duplicates(self, sample_interaction):
        """Same partner is not returned twice."""
        pdb.save_interaction("ATXN3", "VCP", sample_interaction)
        interactions = pdb.get_all_interactions("ATXN3")
        partners = [i.get("primary") for i in interactions]
        assert partners.count("VCP") == 1

    def test_multiple_partners(self):
        """Multiple interaction partners are returned."""
        pdb.save_interaction("ATXN3", "VCP", {"primary": "VCP", "confidence": 0.9})
        pdb.save_interaction("ATXN3", "HDAC6", {"primary": "HDAC6", "confidence": 0.8})
        interactions = pdb.get_all_interactions("ATXN3")
        partners = {i.get("primary") for i in interactions}
        assert partners == {"VCP", "HDAC6"}


# ---------------------------------------------------------------------------
# update_protein_metadata tests
# ---------------------------------------------------------------------------


class TestUpdateProteinMetadata:
    def test_creates_metadata_file(self, sample_interaction):
        """Metadata file is created on first update."""
        pdb.save_interaction("ATXN3", "VCP", sample_interaction)
        result = pdb.update_protein_metadata("ATXN3")
        assert result is True
        meta = pdb.get_protein_metadata("ATXN3")
        assert meta is not None
        assert meta["protein"] == "ATXN3"
        assert meta["query_count"] == 1

    def test_increments_query_count(self, sample_interaction):
        """Repeated updates increment query_count."""
        pdb.save_interaction("ATXN3", "VCP", sample_interaction)
        pdb.update_protein_metadata("ATXN3")
        pdb.update_protein_metadata("ATXN3")
        meta = pdb.get_protein_metadata("ATXN3")
        assert meta["query_count"] == 2


# ---------------------------------------------------------------------------
# build_query_snapshot tests
# ---------------------------------------------------------------------------


class TestBuildQuerySnapshot:
    def test_builds_snapshot_structure(self, sample_interaction):
        """Snapshot has correct structure."""
        pdb.save_interaction("ATXN3", "VCP", sample_interaction)
        snapshot = pdb.build_query_snapshot("ATXN3")
        assert "snapshot_json" in snapshot
        assert snapshot["snapshot_json"]["main"] == "ATXN3"
        assert len(snapshot["snapshot_json"]["interactors"]) >= 1

    def test_empty_snapshot_for_unknown(self):
        """Unknown protein returns empty interactors."""
        snapshot = pdb.build_query_snapshot("UNKNOWN")
        assert snapshot["snapshot_json"]["main"] == "UNKNOWN"
        assert snapshot["snapshot_json"]["interactors"] == []


# ---------------------------------------------------------------------------
# list_all_proteins / database_exists tests
# ---------------------------------------------------------------------------


class TestDatabaseInfo:
    def test_list_all_proteins_empty(self):
        """Empty database returns empty list."""
        # PROTEINS_DIR exists but is empty
        assert pdb.list_all_proteins() == []

    def test_list_all_proteins_after_save(self, sample_interaction):
        """Proteins appear after saving interactions."""
        pdb.save_interaction("ATXN3", "VCP", sample_interaction)
        proteins = pdb.list_all_proteins()
        assert "ATXN3" in proteins
        assert "VCP" in proteins

    def test_database_stats(self, sample_interaction):
        """Stats reflect saved interactions."""
        pdb.save_interaction("ATXN3", "VCP", sample_interaction)
        stats = pdb.get_database_stats()
        assert stats["total_proteins"] == 2
        assert stats["total_interaction_files"] == 2
        assert stats["unique_interactions"] == 1


# ---------------------------------------------------------------------------
# delete_protein tests
# ---------------------------------------------------------------------------


class TestDeleteProtein:
    def test_delete_removes_protein_dir(self, sample_interaction):
        """Deleting removes the protein directory."""
        pdb.save_interaction("ATXN3", "VCP", sample_interaction)
        result = pdb.delete_protein("ATXN3")
        assert result is True
        assert not (pdb.PROTEINS_DIR / "ATXN3").exists()

    def test_delete_removes_symmetric_entries(self, sample_interaction):
        """Deleting also removes symmetric interaction from partner."""
        pdb.save_interaction("ATXN3", "VCP", sample_interaction)
        pdb.delete_protein("ATXN3")
        assert not (pdb.PROTEINS_DIR / "VCP" / "interactions" / "ATXN3.json").exists()

    def test_delete_nonexistent_returns_false(self):
        """Deleting nonexistent protein returns False."""
        assert pdb.delete_protein("NONEXISTENT") is False
