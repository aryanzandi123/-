#!/usr/bin/env python3
"""Tests for async utilities."""

import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.pathway_v2.async_utils import (
    run_parallel,
    chunk_list,
    parallel_llm_calls,
    MAX_CONCURRENT_FLASH
)


def test_chunk_list():
    """Test list chunking."""
    items = [1, 2, 3, 4, 5, 6, 7]
    chunks = chunk_list(items, 3)
    assert chunks == [[1, 2, 3], [4, 5, 6], [7]]


def test_chunk_list_empty():
    """Test empty list chunking."""
    assert chunk_list([], 3) == []


def test_run_parallel_simple():
    """Test parallel execution with simple function."""
    def double(x):
        time.sleep(0.1)  # Simulate work
        return x * 2

    items = [1, 2, 3, 4, 5]
    start = time.time()
    results = run_parallel(items, double, max_concurrent=5, desc="Test")
    elapsed = time.time() - start

    assert results == [2, 4, 6, 8, 10]
    # Should complete in ~0.1s (parallel), not ~0.5s (sequential)
    assert elapsed < 0.3


def test_run_parallel_handles_exceptions():
    """Test that exceptions don't crash entire batch."""
    def maybe_fail(x):
        if x == 3:
            raise ValueError("Intentional failure")
        return x * 2

    items = [1, 2, 3, 4, 5]
    results = run_parallel(items, maybe_fail, max_concurrent=5, desc="Test")

    assert results[0] == 2
    assert results[1] == 4
    assert isinstance(results[2], ValueError)
    assert results[3] == 8
    assert results[4] == 10


