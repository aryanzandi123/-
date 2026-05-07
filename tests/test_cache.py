#!/usr/bin/env python3
"""Tests for pathway cache."""

import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.pathway_v2.cache import PathwayCache


def test_parent_cache():
    """Test parent caching."""
    cache = PathwayCache(load_existing=False)

    assert cache.get_parent("Autophagy") is None
    assert not cache.has_parent("Autophagy")

    cache.set_parent("Autophagy", "Protein Quality Control")

    assert cache.get_parent("Autophagy") == "Protein Quality Control"
    assert cache.has_parent("Autophagy")


def test_case_insensitive():
    """Test that cache is case-insensitive."""
    cache = PathwayCache(load_existing=False)

    cache.set_parent("AUTOPHAGY", "Protein Quality Control")

    assert cache.get_parent("autophagy") == "Protein Quality Control"
    assert cache.get_parent("Autophagy") == "Protein Quality Control"


def test_siblings_cache():
    """Test siblings caching."""
    cache = PathwayCache(load_existing=False)

    siblings = [{"name": "Apoptosis"}, {"name": "Necrosis"}]
    cache.set_siblings("Cell Death", siblings)

    assert cache.get_siblings("Cell Death") == siblings
    assert cache.has_siblings("Cell Death")


def test_persistence():
    """Test save and load from disk."""
    with tempfile.TemporaryDirectory() as tmpdir:
        cache_file = Path(tmpdir) / "test_cache.json"

        # Save
        cache1 = PathwayCache(cache_file=cache_file, load_existing=False)
        cache1.set_parent("Autophagy", "Protein Quality Control")
        cache1.save_to_disk()

        # Load
        cache2 = PathwayCache(cache_file=cache_file, load_existing=True)
        assert cache2.get_parent("Autophagy") == "Protein Quality Control"


def test_stats():
    """Test cache statistics."""
    cache = PathwayCache(load_existing=False)

    cache.set_parent("A", "B")
    cache.set_parent("C", "D")
    cache.set_siblings("X", [{"name": "Y"}])

    stats = cache.stats()
    assert stats["total"] == 3
    assert stats["parents"] == 2
    assert stats["siblings"] == 1


