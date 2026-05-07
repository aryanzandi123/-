# Pipeline Performance Optimization - Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Reduce pipeline runtime from ~60 minutes to ~10 minutes via parallel API calls with rate limiting.

**Architecture:** Async/await pattern with semaphore-based concurrency control. ThreadPoolExecutor wraps blocking Gemini SDK calls. Simple JSON file cache for parent relationships.

**Tech Stack:** Python asyncio, concurrent.futures.ThreadPoolExecutor, existing google-genai SDK

---

## Task 1: Create Async Utilities Module

**Files:**
- Create: `scripts/pathway_v2/async_utils.py`
- Test: `tests/test_async_utils.py`

### Step 1: Create the async utilities file

Create `scripts/pathway_v2/async_utils.py`:

```python
#!/usr/bin/env python3
"""
Async Utilities for Parallel LLM Calls
======================================
Provides semaphore-controlled parallel execution for Gemini API calls.
"""

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import List, Callable, Any, TypeVar, Optional
from functools import partial

logger = logging.getLogger(__name__)

# ==============================================================================
# CONFIGURATION
# ==============================================================================

# Max concurrent API calls (safe with 1000 RPM limit)
MAX_CONCURRENT_FLASH = 15  # For gemini-3-flash-preview
MAX_CONCURRENT_PRO = 4     # For gemini-3.1-pro-preview (more conservative)

# Thread pool for blocking calls
_executor: Optional[ThreadPoolExecutor] = None


def get_executor() -> ThreadPoolExecutor:
    """Get or create the shared thread pool executor."""
    global _executor
    if _executor is None:
        _executor = ThreadPoolExecutor(max_workers=20, thread_name_prefix="llm_")
    return _executor


def shutdown_executor():
    """Shutdown the thread pool executor."""
    global _executor
    if _executor is not None:
        _executor.shutdown(wait=True)
        _executor = None


# ==============================================================================
# PARALLEL EXECUTION
# ==============================================================================

T = TypeVar('T')


async def run_in_executor(func: Callable[..., T], *args, **kwargs) -> T:
    """Run a blocking function in the thread pool executor."""
    loop = asyncio.get_event_loop()
    if kwargs:
        func = partial(func, **kwargs)
    return await loop.run_in_executor(get_executor(), func, *args)


async def parallel_llm_calls(
    items: List[Any],
    call_fn: Callable[[Any], Any],
    max_concurrent: int = MAX_CONCURRENT_FLASH,
    desc: str = "Processing"
) -> List[Any]:
    """
    Run LLM calls in parallel with semaphore-based rate limiting.

    Args:
        items: List of items to process
        call_fn: Function to call for each item (blocking is OK)
        max_concurrent: Maximum concurrent calls
        desc: Description for logging

    Returns:
        List of results (or Exception objects for failed calls)
    """
    if not items:
        return []

    semaphore = asyncio.Semaphore(max_concurrent)
    total = len(items)
    completed = 0

    async def bounded_call(idx: int, item: Any) -> Any:
        nonlocal completed
        async with semaphore:
            try:
                result = await run_in_executor(call_fn, item)
                completed += 1
                if completed % 5 == 0 or completed == total:
                    logger.info(f"{desc}: {completed}/{total} complete")
                return result
            except Exception as e:
                completed += 1
                logger.warning(f"{desc} item {idx} failed: {e}")
                return e

    logger.info(f"{desc}: Starting {total} items (max {max_concurrent} concurrent)")

    tasks = [bounded_call(i, item) for i, item in enumerate(items)]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # Count successes/failures
    successes = sum(1 for r in results if not isinstance(r, Exception))
    failures = total - successes
    logger.info(f"{desc}: Complete. {successes} succeeded, {failures} failed")

    return results


async def parallel_llm_calls_with_callback(
    items: List[Any],
    call_fn: Callable[[Any], Any],
    on_result: Callable[[int, Any, Any], None],
    max_concurrent: int = MAX_CONCURRENT_FLASH,
    desc: str = "Processing"
) -> List[Any]:
    """
    Like parallel_llm_calls but calls on_result(idx, item, result) after each completion.
    Useful for streaming results to database.
    """
    if not items:
        return []

    semaphore = asyncio.Semaphore(max_concurrent)
    results = [None] * len(items)

    async def bounded_call(idx: int, item: Any):
        async with semaphore:
            try:
                result = await run_in_executor(call_fn, item)
                results[idx] = result
                on_result(idx, item, result)
            except Exception as e:
                results[idx] = e
                on_result(idx, item, e)

    logger.info(f"{desc}: Starting {len(items)} items")
    await asyncio.gather(*[bounded_call(i, item) for i, item in enumerate(items)])
    return results


def run_parallel(
    items: List[Any],
    call_fn: Callable[[Any], Any],
    max_concurrent: int = MAX_CONCURRENT_FLASH,
    desc: str = "Processing"
) -> List[Any]:
    """
    Synchronous wrapper for parallel_llm_calls.
    Use this from non-async code.
    """
    return asyncio.run(parallel_llm_calls(items, call_fn, max_concurrent, desc))


# ==============================================================================
# BATCHING UTILITIES
# ==============================================================================

def chunk_list(items: List[Any], size: int) -> List[List[Any]]:
    """Split a list into chunks of specified size."""
    return [items[i:i + size] for i in range(0, len(items), size)]


def flatten_results(results: List[List[Any]]) -> List[Any]:
    """Flatten a list of lists into a single list."""
    return [item for sublist in results for item in sublist]
```

### Step 2: Create basic test file

Create `tests/test_async_utils.py`:

```python
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


if __name__ == "__main__":
    test_chunk_list()
    test_chunk_list_empty()
    test_run_parallel_simple()
    test_run_parallel_handles_exceptions()
    print("All tests passed!")
```

### Step 3: Run tests to verify

Run: `cd "/Users/aryanzandi/Documents/5008 Backup Avoid Messy [Older Than 8] Retry [11-14]/5008 copy 11 [WORKING VERSION 1]" && python tests/test_async_utils.py`

Expected: "All tests passed!"

### Step 4: Commit

```bash
git add scripts/pathway_v2/async_utils.py tests/test_async_utils.py
git commit -m "feat: add async utilities for parallel LLM calls"
```

---

## Task 2: Create Cache Module

**Files:**
- Create: `scripts/pathway_v2/cache.py`
- Test: `tests/test_cache.py`

### Step 1: Create the cache module

Create `scripts/pathway_v2/cache.py`:

```python
#!/usr/bin/env python3
"""
Pathway Cache - Avoid Redundant LLM Calls
=========================================
Simple in-memory cache with JSON file persistence.
"""

import json
import logging
from pathlib import Path
from threading import Lock
from typing import Optional, List, Dict, Any

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).parent.parent.parent
CACHE_FILE = PROJECT_ROOT / "cache" / "pathway_hierarchy_cache.json"


class PathwayCache:
    """
    Simple cache for parent-child relationships and sibling lists.
    Thread-safe with optional disk persistence.
    """

    def __init__(self, cache_file: Path = CACHE_FILE, load_existing: bool = True):
        self._cache: Dict[str, Any] = {}
        self._lock = Lock()
        self._cache_file = cache_file
        self._dirty = False

        if load_existing:
            self._load_from_disk()

    def _normalize_key(self, name: str) -> str:
        """Normalize pathway name for cache key."""
        return name.strip().lower()

    # -------------------------------------------------------------------------
    # Parent Cache
    # -------------------------------------------------------------------------

    def get_parent(self, child_name: str) -> Optional[str]:
        """Get cached parent for a pathway, or None if not cached."""
        key = f"parent:{self._normalize_key(child_name)}"
        return self._cache.get(key)

    def set_parent(self, child_name: str, parent_name: str):
        """Cache a parent relationship."""
        with self._lock:
            key = f"parent:{self._normalize_key(child_name)}"
            self._cache[key] = parent_name
            self._dirty = True

    def has_parent(self, child_name: str) -> bool:
        """Check if parent is cached."""
        key = f"parent:{self._normalize_key(child_name)}"
        return key in self._cache

    # -------------------------------------------------------------------------
    # Siblings Cache
    # -------------------------------------------------------------------------

    def get_siblings(self, parent_name: str) -> Optional[List[Dict]]:
        """Get cached siblings for a parent, or None if not cached."""
        key = f"siblings:{self._normalize_key(parent_name)}"
        return self._cache.get(key)

    def set_siblings(self, parent_name: str, siblings: List[Dict]):
        """Cache sibling list for a parent."""
        with self._lock:
            key = f"siblings:{self._normalize_key(parent_name)}"
            self._cache[key] = siblings
            self._dirty = True

    def has_siblings(self, parent_name: str) -> bool:
        """Check if siblings are cached."""
        key = f"siblings:{self._normalize_key(parent_name)}"
        return key in self._cache

    # -------------------------------------------------------------------------
    # Persistence
    # -------------------------------------------------------------------------

    def save_to_disk(self):
        """Persist cache to JSON file."""
        if not self._dirty:
            return

        try:
            self._cache_file.parent.mkdir(parents=True, exist_ok=True)
            with self._lock:
                with open(self._cache_file, 'w', encoding='utf-8') as f:
                    json.dump(self._cache, f, indent=2, ensure_ascii=False)
                self._dirty = False
            logger.info(f"Cache saved to {self._cache_file} ({len(self._cache)} entries)")
        except Exception as e:
            logger.warning(f"Failed to save cache: {e}")

    def _load_from_disk(self):
        """Load cache from JSON file if exists."""
        if not self._cache_file.exists():
            logger.debug(f"No cache file at {self._cache_file}")
            return

        try:
            with open(self._cache_file, 'r', encoding='utf-8') as f:
                self._cache = json.load(f)
            logger.info(f"Loaded cache from {self._cache_file} ({len(self._cache)} entries)")
        except Exception as e:
            logger.warning(f"Failed to load cache: {e}")
            self._cache = {}

    def clear(self):
        """Clear the cache."""
        with self._lock:
            self._cache.clear()
            self._dirty = True

    def stats(self) -> Dict[str, int]:
        """Get cache statistics."""
        parent_count = sum(1 for k in self._cache if k.startswith("parent:"))
        sibling_count = sum(1 for k in self._cache if k.startswith("siblings:"))
        return {
            "total": len(self._cache),
            "parents": parent_count,
            "siblings": sibling_count
        }


# Global instance
_pathway_cache: Optional[PathwayCache] = None


def get_pathway_cache() -> PathwayCache:
    """Get or create the global pathway cache instance."""
    global _pathway_cache
    if _pathway_cache is None:
        _pathway_cache = PathwayCache()
    return _pathway_cache


def save_cache():
    """Save the global cache to disk."""
    if _pathway_cache is not None:
        _pathway_cache.save_to_disk()
```

### Step 2: Create test file

Create `tests/test_cache.py`:

```python
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


if __name__ == "__main__":
    test_parent_cache()
    test_case_insensitive()
    test_siblings_cache()
    test_persistence()
    test_stats()
    print("All tests passed!")
```

### Step 3: Run tests

Run: `cd "/Users/aryanzandi/Documents/5008 Backup Avoid Messy [Older Than 8] Retry [11-14]/5008 copy 11 [WORKING VERSION 1]" && python tests/test_cache.py`

Expected: "All tests passed!"

### Step 4: Commit

```bash
git add scripts/pathway_v2/cache.py tests/test_cache.py
git commit -m "feat: add pathway cache for avoiding redundant LLM calls"
```

---

## Task 3: Add Async Wrapper to llm_utils.py

**Files:**
- Modify: `scripts/pathway_v2/llm_utils.py`

### Step 1: Add async wrapper function

Add to the END of `scripts/pathway_v2/llm_utils.py`:

```python
# ==============================================================================
# CACHED LLM CALLS
# ==============================================================================

def _call_gemini_json_cached(
    prompt: str,
    cache_key: str = None,
    cache_type: str = "parent",  # "parent" or "siblings"
    api_key: str = None,
    max_retries: int = 3,
    temperature: float = 0.3,
    max_output_tokens: int = 8192
) -> dict:
    """
    Call Gemini with optional caching.

    If cache_key is provided and cache_type is "parent", checks PathwayCache first.
    """
    if cache_key:
        from scripts.pathway_v2.cache import get_pathway_cache
        cache = get_pathway_cache()

        if cache_type == "parent":
            cached = cache.get_parent(cache_key)
            if cached:
                logger.info(f"  Cache hit for parent of '{cache_key}'")
                return {"child": cache_key, "parent": cached, "_cached": True}
        elif cache_type == "siblings":
            cached = cache.get_siblings(cache_key)
            if cached:
                logger.info(f"  Cache hit for siblings of '{cache_key}'")
                return {"siblings": cached, "_cached": True}

    # Call LLM
    result = _call_gemini_json(
        prompt=prompt,
        api_key=api_key,
        max_retries=max_retries,
        temperature=temperature,
        max_output_tokens=max_output_tokens
    )

    # Cache result if successful
    if cache_key and result:
        from scripts.pathway_v2.cache import get_pathway_cache
        cache = get_pathway_cache()

        if cache_type == "parent" and result.get("parent"):
            cache.set_parent(cache_key, result["parent"])
        elif cache_type == "siblings" and result.get("siblings"):
            cache.set_siblings(cache_key, result["siblings"])

    return result
```

### Step 2: Verify import works

Run: `cd "/Users/aryanzandi/Documents/5008 Backup Avoid Messy [Older Than 8] Retry [11-14]/5008 copy 11 [WORKING VERSION 1]" && python -c "from scripts.pathway_v2.llm_utils import _call_gemini_json_cached; print('Import OK')"`

Expected: "Import OK"

### Step 3: Commit

```bash
git add scripts/pathway_v2/llm_utils.py
git commit -m "feat: add cached LLM call wrapper"
```

---

## Task 4: Optimize Step 5 (Sibling Discovery) - Easiest Win

**Files:**
- Modify: `scripts/pathway_v2/step5_discover_siblings.py`

### Step 1: Rewrite step5 with parallel execution

Replace the ENTIRE content of `scripts/pathway_v2/step5_discover_siblings.py`:

```python
#!/usr/bin/env python3
"""
Step 5: Discover Siblings (Parallel Version)
=============================================
Goal: Populate the tree with related pathways (siblings) to build a complete biological taxonomy.

OPTIMIZED: All parent pathways processed in parallel (they are independent).
Expected speedup: ~15 min -> ~30-45 seconds
"""

import sys
import logging
from pathlib import Path
from typing import List, Dict, Any, Optional

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

from scripts.pathway_v2.llm_utils import _call_gemini_json_cached
from scripts.pathway_v2.async_utils import run_parallel, MAX_CONCURRENT_FLASH
from scripts.pathway_v2.cache import get_pathway_cache, save_cache

SIBLING_PROMPT = """You are a biological taxonomy expert.
Task: Identify the SIBLING pathways of "{child_name}" that also fall under the parent category "{parent_name}".

## CONTEXT
Parent: {parent_name}
Child: {child_name}

## INSTRUCTIONS
1. List significant biological pathways that are "siblings" (other types/subprocesses of the parent).
2. Use standard terminology.
3. Limit to top 5-7 most relevant siblings.

## RESPONSE FORMAT (Strict JSON)
{{
  "siblings": [
     {{ "name": "Sibling Name", "description": "Brief desc" }}
  ]
}}
Respond with ONLY the JSON.
"""


def _discover_siblings_for_parent(parent_data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Discover siblings for a single parent. Called in parallel.

    Args:
        parent_data: Dict with 'parent_id', 'parent_name', 'sample_child_name'

    Returns:
        Dict with 'parent_id', 'siblings', 'error'
    """
    parent_id = parent_data['parent_id']
    parent_name = parent_data['parent_name']
    sample_child = parent_data.get('sample_child_name', 'a child pathway')

    try:
        resp = _call_gemini_json_cached(
            SIBLING_PROMPT.format(child_name=sample_child, parent_name=parent_name),
            cache_key=parent_name,
            cache_type="siblings",
            temperature=0.3
        )

        siblings = resp.get('siblings', [])
        return {
            'parent_id': parent_id,
            'parent_name': parent_name,
            'siblings': siblings,
            'error': None,
            'cached': resp.get('_cached', False)
        }
    except Exception as e:
        logger.error(f"Error discovering siblings for {parent_name}: {e}")
        return {
            'parent_id': parent_id,
            'parent_name': parent_name,
            'siblings': [],
            'error': str(e),
            'cached': False
        }


def discover_siblings():
    """Discover sibling pathways for ALL parent pathways in PARALLEL."""
    try:
        from app import app, db
        from models import Pathway, PathwayParent
    except ImportError as e:
        logger.error(f"Failed to import app/db: {e}")
        return

    with app.app_context():
        # Get ALL unique parent pathways that have at least one child
        parent_ids = db.session.query(PathwayParent.parent_pathway_id).distinct().all()
        parent_ids = [pid[0] for pid in parent_ids]

        logger.info(f"Found {len(parent_ids)} parent pathways to check for sibling completeness.")

        # Build list of parent data for parallel processing
        parent_data_list = []
        for parent_id in parent_ids:
            parent = Pathway.query.get(parent_id)
            if not parent:
                continue

            # Get existing children names for this parent
            existing_links = PathwayParent.query.filter_by(parent_pathway_id=parent_id).all()
            existing_child_names = {link.child.name for link in existing_links if link.child}

            if not existing_child_names:
                continue

            # Use first child as sample for context
            sample_child_name = list(existing_child_names)[0]

            parent_data_list.append({
                'parent_id': parent_id,
                'parent_name': parent.name,
                'sample_child_name': sample_child_name,
                'existing_children': existing_child_names
            })

        if not parent_data_list:
            logger.info("No parents to process.")
            return

        logger.info(f"Processing {len(parent_data_list)} parents in parallel...")

        # Run all sibling discovery calls in parallel
        results = run_parallel(
            parent_data_list,
            _discover_siblings_for_parent,
            max_concurrent=MAX_CONCURRENT_FLASH,
            desc="Sibling discovery"
        )

        # Process results and create pathways
        total_siblings_added = 0
        cache_hits = 0

        for i, result in enumerate(results):
            if isinstance(result, Exception):
                logger.error(f"Failed for parent: {result}")
                continue

            parent_id = result['parent_id']
            parent_name = result['parent_name']
            siblings = result.get('siblings', [])

            if result.get('cached'):
                cache_hits += 1

            if result.get('error'):
                continue

            # Get existing children for this parent (need to re-query to get current state)
            existing_links = PathwayParent.query.filter_by(parent_pathway_id=parent_id).all()
            existing_child_names = {link.child.name for link in existing_links if link.child}

            count = 0
            for sib in siblings:
                name = sib.get('name')
                if not name or name in existing_child_names:
                    continue

                # Create pathway if doesn't exist
                existing_pw = Pathway.query.filter_by(name=name).first()
                if not existing_pw:
                    parent = Pathway.query.get(parent_id)
                    existing_pw = Pathway(
                        name=name,
                        description=sib.get('description'),
                        hierarchy_level=parent.hierarchy_level + 1 if parent else 1,
                        is_leaf=True,
                        ai_generated=True
                    )
                    db.session.add(existing_pw)
                    db.session.commit()

                # Create parent link if doesn't exist
                if not PathwayParent.query.filter_by(
                    child_pathway_id=existing_pw.id,
                    parent_pathway_id=parent_id
                ).first():
                    link = PathwayParent(
                        child_pathway_id=existing_pw.id,
                        parent_pathway_id=parent_id,
                        relationship_type='is_a'
                    )
                    db.session.add(link)
                    count += 1

            db.session.commit()

            if count > 0:
                logger.info(f"  Added {count} siblings under '{parent_name}'")
                total_siblings_added += count

        # Save cache at end
        save_cache()

        logger.info(f"\n{'='*60}")
        logger.info(f"Step 5 Complete (Parallel):")
        logger.info(f"  Parents processed: {len(parent_data_list)}")
        logger.info(f"  Cache hits: {cache_hits}")
        logger.info(f"  Total siblings added: {total_siblings_added}")
        logger.info(f"{'='*60}\n")


if __name__ == "__main__":
    discover_siblings()
```

### Step 2: Verify syntax

Run: `cd "/Users/aryanzandi/Documents/5008 Backup Avoid Messy [Older Than 8] Retry [11-14]/5008 copy 11 [WORKING VERSION 1]" && python3 -m py_compile scripts/pathway_v2/step5_discover_siblings.py && echo "Syntax OK"`

Expected: "Syntax OK"

### Step 3: Commit

```bash
git add scripts/pathway_v2/step5_discover_siblings.py
git commit -m "perf: parallelize Step 5 sibling discovery

- All parent pathways processed simultaneously (was sequential)
- Cache integration for repeat runs
- Expected: ~15 min -> ~30-45 seconds"
```

---

## Task 5: Optimize Step 4 (Hierarchy Building)

**Files:**
- Modify: `scripts/pathway_v2/step4_build_hierarchy_backwards.py`

### Step 1: Rewrite step4 with two-phase parallel climbing

Replace the ENTIRE content of `scripts/pathway_v2/step4_build_hierarchy_backwards.py`:

```python
#!/usr/bin/env python3
"""
Step 4: Build Hierarchy Backwards (Parallel Version)
=====================================================
Goal: For every NEW finalized pathway, build the hierarchy chain backwards until a Root or Existing Pathway is reached.

OPTIMIZED: Two-phase parallel climbing
- Phase A: All leaf pathways ask for parent SIMULTANEOUSLY
- Phase B: Dedupe, then process next level in parallel
- Repeat until all chains reach roots

Expected speedup: ~25 min -> ~2-3 minutes
"""

import sys
import logging
from pathlib import Path
from typing import List, Dict, Set, Any, Optional

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

from scripts.pathway_v2.llm_utils import _call_gemini_json_cached
from scripts.pathway_v2.async_utils import run_parallel, MAX_CONCURRENT_FLASH
from scripts.pathway_v2.cache import get_pathway_cache, save_cache
from scripts.pathway_v2.step1_init_roots import ROOT_PATHWAYS

ROOT_NAMES = {r['name'] for r in ROOT_PATHWAYS}

PARENT_PROMPT = """You are a biological taxonomy expert building a detailed pathway hierarchy.
Task: Identify the IMMEDIATE biological parent pathway for: "{child_name}".

## CONTEXT
We are building a DEEP hierarchy tree with multiple levels between specific pathways and roots.
The ROOTS are: {roots_list}

## CRITICAL RULES
1. **DO NOT** jump directly to a Root unless "{child_name}" is truly a top-level biological category.
2. Most pathways should have 3-6 levels between them and a Root.
3. The parent must be ONE LEVEL more general than the child - not multiple levels.

## EXAMPLES OF CORRECT HIERARCHIES
- "p53 Signaling" -> "Apoptosis Signaling" (NOT directly to "Cell Death" or "Cellular Signaling")
- "Aggrephagy" -> "Selective Macroautophagy" -> "Macroautophagy" -> "Autophagy" -> "Protein Quality Control"
- "mTOR Signaling" -> "Nutrient Sensing" -> "Growth Factor Signaling" -> "Cellular Signaling"
- "HDAC6-mediated Deacetylation" -> "Histone Deacetylation" -> "Epigenetic Regulation" -> "Gene Expression" -> "Cellular Signaling"

## EXAMPLES OF WRONG (TOO SHALLOW) HIERARCHIES
- "p53 Signaling" -> "Cellular Signaling" (WRONG - skips intermediate levels)
- "Aggrephagy" -> "Protein Quality Control" (WRONG - skips 4 intermediate levels)
- "HDAC6 Activity" -> "Metabolism" (WRONG - too vague, skips specificity)

## INSTRUCTIONS
1. Name the IMMEDIATE parent (exactly one level broader).
2. The parent must PERFECTLY ENCAPSULATE the child but be only SLIGHTLY more general.
3. Think: "What TYPE of thing is {child_name}?" The answer is the parent.
4. Only return a Root if {child_name} is genuinely a level-1 category (like "Apoptosis" under "Cell Death").

## RESPONSE FORMAT (Strict JSON)
{{
  "child": "{child_name}",
  "parent": "Name of Immediate Parent Pathway",
  "reasoning": "Explain why this is the direct parent, not a more distant ancestor"
}}
Respond with ONLY the JSON.
"""


def _get_parent_for_pathway(pathway_name: str) -> Dict[str, Any]:
    """
    Get parent for a single pathway. Called in parallel.
    """
    roots_str = ", ".join(ROOT_NAMES)

    try:
        resp = _call_gemini_json_cached(
            PARENT_PROMPT.format(child_name=pathway_name, roots_list=roots_str),
            cache_key=pathway_name,
            cache_type="parent",
            temperature=0.1
        )

        parent_name = resp.get('parent')

        return {
            'child': pathway_name,
            'parent': parent_name,
            'reasoning': resp.get('reasoning', ''),
            'error': None,
            'cached': resp.get('_cached', False)
        }
    except Exception as e:
        logger.error(f"Error getting parent for {pathway_name}: {e}")
        return {
            'child': pathway_name,
            'parent': None,
            'reasoning': '',
            'error': str(e),
            'cached': False
        }


def _run_recovery_for_missing_steps() -> None:
    """Run recovery for any interactions missing step2 or step3 assignments."""
    try:
        from app import app, db
        from models import Interaction
    except ImportError:
        return

    with app.app_context():
        # Check for missing step2_proposal
        missing_step2 = Interaction.query.filter(
            ~Interaction.data.has_key('step2_proposal')
        ).all()

        if missing_step2:
            logger.warning(f"Found {len(missing_step2)} interactions missing step2_proposal. Running recovery...")
            from scripts.pathway_v2.step2_assign_initial_terms import assign_initial_terms_for_interactions
            assign_initial_terms_for_interactions(missing_step2)

        # Check for missing step3_finalized_pathway
        missing_step3 = Interaction.query.filter(
            Interaction.data.has_key('step2_proposal') &
            ~Interaction.data.has_key('step3_finalized_pathway')
        ).all()

        if missing_step3:
            logger.warning(f"Found {len(missing_step3)} interactions missing step3_finalized_pathway. Running recovery...")
            from scripts.pathway_v2.step3_refine_pathways import refine_pathways_for_interactions
            refine_pathways_for_interactions(missing_step3)


def build_hierarchy() -> None:
    """Build pathway hierarchy using two-phase parallel climbing."""
    try:
        from app import app, db
        from models import Interaction, Pathway, PathwayParent, PathwayInteraction
    except ImportError as e:
        logger.error(f"Failed to import app/db: {e}")
        return

    # First run recovery for any missing steps
    _run_recovery_for_missing_steps()

    with app.app_context():
        # Get all interactions with finalized pathways
        interactions = Interaction.query.filter(
            Interaction.data.has_key('step3_finalized_pathway') |
            Interaction.data.has_key('step3_function_pathways')
        ).all()

        # Build term -> interactions map
        term_to_interactions: Dict[str, List] = {}
        interaction_to_terms: Dict[int, Set[str]] = {}

        for i in interactions:
            interaction_terms = set()

            # Method 1: Function-level pathways (preferred)
            func_pathways = i.data.get('step3_function_pathways', [])
            for fp in func_pathways:
                pw = fp.get('finalized_pathway')
                if pw:
                    interaction_terms.add(pw)

            # Method 2: Function's own pathway field
            for func in i.data.get('functions', []):
                pw = func.get('pathway')
                if isinstance(pw, str) and pw:
                    interaction_terms.add(pw)
                elif isinstance(pw, dict) and pw.get('name'):
                    interaction_terms.add(pw['name'])

            # Method 3: Fallback to interaction-level
            if not interaction_terms:
                primary = i.data.get('step3_finalized_pathway')
                if primary:
                    interaction_terms.add(primary)

            # Add interaction to each of its pathways
            for term in interaction_terms:
                if term not in term_to_interactions:
                    term_to_interactions[term] = []
                term_to_interactions[term].append(i)

            interaction_to_terms[i.id] = interaction_terms

        unique_terms = list(term_to_interactions.keys())
        logger.info(f"Processing hierarchy for {len(unique_terms)} unique terms from {len(interactions)} interactions.")

        # ------------------------------------------------------------------
        # Step 1: Create leaf pathways and PathwayInteraction links
        # ------------------------------------------------------------------
        total_pathway_interactions_created = 0

        for leaf_name in unique_terms:
            # Create Leaf Pathway if missing
            curr_pw = Pathway.query.filter_by(name=leaf_name).first()
            if not curr_pw:
                curr_pw = Pathway(
                    name=leaf_name,
                    hierarchy_level=10,  # Placeholder level
                    is_leaf=True,
                    ai_generated=True
                )
                db.session.add(curr_pw)
                db.session.commit()

            # Assign Interactions to this pathway
            interactions_for_term = term_to_interactions[leaf_name]
            for ix in interactions_for_term:
                existing_link = PathwayInteraction.query.filter_by(
                    pathway_id=curr_pw.id,
                    interaction_id=ix.id
                ).first()
                if not existing_link:
                    pi = PathwayInteraction(
                        pathway_id=curr_pw.id,
                        interaction_id=ix.id,
                        assignment_method='AI_V2_Step4'
                    )
                    db.session.add(pi)
                    total_pathway_interactions_created += 1
            db.session.commit()

        logger.info(f"Created {total_pathway_interactions_created} PathwayInteraction links")

        # ------------------------------------------------------------------
        # Step 2: Two-phase parallel hierarchy climbing
        # ------------------------------------------------------------------

        # Find pathways that need parents (not roots, don't have parent yet)
        def get_pathways_needing_parents() -> List[str]:
            """Get pathway names that need parent assignment."""
            needs_parent = []
            for term in unique_terms:
                if term in ROOT_NAMES:
                    continue
                pw = Pathway.query.filter_by(name=term).first()
                if not pw:
                    continue
                existing_parent = PathwayParent.query.filter_by(child_pathway_id=pw.id).first()
                if not existing_parent:
                    needs_parent.append(term)
            return needs_parent

        pending = get_pathways_needing_parents()
        all_processed = set()
        level = 1
        total_cache_hits = 0

        while pending:
            logger.info(f"\n--- Hierarchy Level {level}: {len(pending)} pathways ---")

            # Process all pending pathways in parallel
            results = run_parallel(
                pending,
                _get_parent_for_pathway,
                max_concurrent=MAX_CONCURRENT_FLASH,
                desc=f"Level {level} parent lookup"
            )

            # Process results
            next_level_candidates = set()

            for result in results:
                if isinstance(result, Exception):
                    continue

                child_name = result['child']
                parent_name = result.get('parent')

                if result.get('cached'):
                    total_cache_hits += 1

                if not parent_name or parent_name == child_name:
                    logger.warning(f"  Invalid parent '{parent_name}' for '{child_name}'")
                    continue

                # Prevent cycles
                if parent_name in all_processed and parent_name not in ROOT_NAMES:
                    # Check if this would create a cycle
                    continue

                logger.info(f"  '{child_name}' -> '{parent_name}'")

                # Get or create child pathway
                child_pw = Pathway.query.filter_by(name=child_name).first()
                if not child_pw:
                    continue

                # Check if already has parent
                existing_parent = PathwayParent.query.filter_by(child_pathway_id=child_pw.id).first()
                if existing_parent:
                    continue

                # Get or create parent pathway
                parent_pw = Pathway.query.filter_by(name=parent_name).first()
                if not parent_pw:
                    parent_pw = Pathway(
                        name=parent_name,
                        hierarchy_level=0 if parent_name in ROOT_NAMES else max(0, child_pw.hierarchy_level - 1),
                        is_leaf=False,
                        ai_generated=True
                    )
                    db.session.add(parent_pw)
                    db.session.commit()

                # Create parent-child link
                link = PathwayParent(
                    child_pathway_id=child_pw.id,
                    parent_pathway_id=parent_pw.id,
                    relationship_type='is_a'
                )
                db.session.add(link)
                db.session.commit()

                all_processed.add(child_name)

                # Add parent to next level if not a root and not already processed
                if parent_name not in ROOT_NAMES and parent_name not in all_processed:
                    # Check if parent needs a parent
                    parent_has_parent = PathwayParent.query.filter_by(child_pathway_id=parent_pw.id).first()
                    if not parent_has_parent:
                        next_level_candidates.add(parent_name)

            # Prepare next level
            pending = list(next_level_candidates)
            level += 1

            # Safety limit
            if level > 10:
                logger.warning("Reached maximum hierarchy depth, stopping")
                break

        # ------------------------------------------------------------------
        # Step 3: Final verification and fallback
        # ------------------------------------------------------------------

        unlinked = []
        for i in interactions:
            has_any_link = PathwayInteraction.query.filter_by(interaction_id=i.id).first()
            if not has_any_link:
                unlinked.append(i)

        if unlinked:
            logger.warning(f"Found {len(unlinked)} interactions without PathwayInteraction records. Creating fallback links...")
            fallback_pw = Pathway.query.filter_by(name='Protein Quality Control').first()
            if fallback_pw:
                for ix in unlinked:
                    pi = PathwayInteraction(
                        pathway_id=fallback_pw.id,
                        interaction_id=ix.id,
                        assignment_method='Fallback_Step4'
                    )
                    db.session.add(pi)
                db.session.commit()
                logger.info(f"  Created {len(unlinked)} fallback PathwayInteraction links.")

        # Save cache
        save_cache()

        # Final report
        logger.info(f"\n{'='*60}")
        logger.info(f"Step 4 Complete (Parallel):")
        logger.info(f"  Unique pathway terms: {len(unique_terms)}")
        logger.info(f"  PathwayInteraction records created: {total_pathway_interactions_created}")
        logger.info(f"  Hierarchy levels processed: {level - 1}")
        logger.info(f"  Cache hits: {total_cache_hits}")
        logger.info(f"  Interactions processed: {len(interactions)}")
        logger.info(f"{'='*60}\n")


if __name__ == "__main__":
    build_hierarchy()
```

### Step 2: Verify syntax

Run: `cd "/Users/aryanzandi/Documents/5008 Backup Avoid Messy [Older Than 8] Retry [11-14]/5008 copy 11 [WORKING VERSION 1]" && python3 -m py_compile scripts/pathway_v2/step4_build_hierarchy_backwards.py && echo "Syntax OK"`

Expected: "Syntax OK"

### Step 3: Commit

```bash
git add scripts/pathway_v2/step4_build_hierarchy_backwards.py
git commit -m "perf: parallelize Step 4 hierarchy building

- Two-phase parallel climbing (all pathways at same level processed together)
- Cache integration for repeat runs
- Expected: ~25 min -> ~2-3 minutes"
```

---

## Task 6: Optimize Step 6 Phases 1-3

**Files:**
- Modify: `scripts/pathway_v2/step6_reorganize_pathways.py`

### Step 1: Add parallel processing to Phase 1 (Deduplication)

In `scripts/pathway_v2/step6_reorganize_pathways.py`, replace the `phase1_deduplication` function (around line 270-390) with:

```python
def phase1_deduplication(db, Pathway, PathwayParent, PathwayInteraction, dry_run: bool) -> PhaseResult:
    """
    Phase 1: Merge duplicate/synonym pathways.
    OPTIMIZED: Parallel LLM calls for merge decisions.
    """
    from scripts.pathway_v2.async_utils import run_parallel, MAX_CONCURRENT_FLASH

    result = PhaseResult(phase_name="Deduplication", success=True)
    logger.info("=" * 60)
    logger.info("PHASE 1: DEDUPLICATION (Parallel)")
    logger.info("=" * 60)

    # Find candidates
    pathways = Pathway.query.all()
    candidates = find_duplicate_candidates(pathways)

    if not candidates:
        logger.info("No duplicate candidates found.")
        return result

    logger.info(f"Found {len(candidates)} candidate duplicate pairs")

    # Group into batches of 3 pairs each
    from scripts.pathway_v2.async_utils import chunk_list
    batches = chunk_list(candidates, 3)

    def process_batch(batch):
        """Process a batch of candidate pairs."""
        pairs_str = "\n".join([f"- Pair: '{p[0]}' vs '{p[1]}'" for p in batch])
        try:
            resp = _call_gemini_json(
                MERGE_PROMPT.format(pairs_list=pairs_str),
                temperature=0.1,
                max_output_tokens=21096
            )
            return {'batch': batch, 'response': resp, 'error': None}
        except Exception as e:
            return {'batch': batch, 'response': None, 'error': str(e)}

    # Process all batches in parallel
    batch_results = run_parallel(
        batches,
        process_batch,
        max_concurrent=MAX_CONCURRENT_FLASH,
        desc="Deduplication batches"
    )

    # Process results
    for batch_result in batch_results:
        if isinstance(batch_result, Exception):
            result.add_error(f"Batch failed: {batch_result}")
            continue

        if batch_result.get('error'):
            result.add_warning(f"Batch error: {batch_result['error']}")
            continue

        resp = batch_result.get('response', {})
        merges = resp.get('merges', [])

        for m in merges:
            if m.get('action') != 'MERGE':
                continue

            canon = m.get('canonical_name')
            name_a = m.get('name_a')
            name_b = m.get('name_b')

            if not canon or not name_a or not name_b:
                continue

            to_keep = canon
            to_drop = name_a if name_a != canon else name_b

            if dry_run:
                logger.info(f"[DRY RUN] MERGE: '{to_drop}' -> '{to_keep}'")
                result.add_change(Change(
                    change_type=ChangeType.MERGE,
                    entity_type='pathway',
                    entity_id=0,
                    old_value=to_drop,
                    new_value=to_keep,
                    reason="Duplicate names"
                ))
                continue

            # Get pathway records
            keep_pw = Pathway.query.filter_by(name=to_keep).first()
            drop_pw = Pathway.query.filter_by(name=to_drop).first()

            if not keep_pw or not drop_pw:
                result.add_warning(f"Could not find pathways for merge: {to_keep}, {to_drop}")
                continue

            # Build and execute migration plan
            plan = build_merge_migration_plan(
                drop_pw.id, keep_pw.id,
                PathwayParent, PathwayInteraction
            )

            success, errors = execute_migration_plan(
                plan, db, Pathway, PathwayParent, PathwayInteraction
            )

            if success:
                result.add_change(Change(
                    change_type=ChangeType.MERGE,
                    entity_type='pathway',
                    entity_id=drop_pw.id,
                    old_value=to_drop,
                    new_value=to_keep,
                    reason="Merged duplicate"
                ))
                logger.info(f"Merged '{to_drop}' into '{to_keep}'")
            else:
                result.add_error(f"Failed to merge {to_drop}: {errors}")

    # Verify no dangling links
    if not dry_run:
        dangling = validate_no_dangling_pathway_links(db, PathwayInteraction, Pathway)
        if dangling:
            result.add_error(f"Found {len(dangling)} dangling PathwayInteraction records after merge")
            result.success = False

    return result
```

### Step 2: Add parallel processing to Phase 2 (Tree Enforcement)

Replace the `phase2_tree_enforcement` function (around line 396-525) with:

```python
def phase2_tree_enforcement(db, Pathway, PathwayParent, dry_run: bool) -> PhaseResult:
    """
    Phase 2: Ensure each pathway has exactly one parent (except roots).
    OPTIMIZED: Parallel LLM calls for multi-parent resolution.
    """
    from scripts.pathway_v2.async_utils import run_parallel, MAX_CONCURRENT_FLASH

    result = PhaseResult(phase_name="Tree Enforcement", success=True)
    logger.info("=" * 60)
    logger.info("PHASE 2: TREE ENFORCEMENT (Parallel)")
    logger.info("=" * 60)

    # Build graph and find multi-parent nodes
    parent_graph = build_parent_graph(PathwayParent)
    multi_parent = detect_multi_parent_nodes(parent_graph)

    if not multi_parent:
        logger.info("No multi-parent pathways found. Tree is valid.")
        return result

    logger.info(f"Found {len(multi_parent)} pathways with multiple parents")

    # Build list of items to process
    items_to_process = []
    for child_id, parent_ids in multi_parent.items():
        child = Pathway.query.get(child_id)
        if not child:
            continue

        parents = [Pathway.query.get(pid) for pid in parent_ids]
        parents = [p for p in parents if p]

        if len(parents) <= 1:
            continue

        items_to_process.append({
            'child_id': child_id,
            'child_name': child.name,
            'parent_ids': parent_ids,
            'parent_names': [p.name for p in parents]
        })

    def pick_best_parent(item):
        """Pick best parent for a child with multiple parents."""
        try:
            resp = _call_gemini_json(
                BEST_PARENT_PROMPT.format(
                    child_name=item['child_name'],
                    parents_list=", ".join(item['parent_names'])
                ),
                temperature=0.1
            )
            return {
                'child_id': item['child_id'],
                'child_name': item['child_name'],
                'parent_ids': item['parent_ids'],
                'parent_names': item['parent_names'],
                'selected': resp.get('selected_parent'),
                'error': None
            }
        except Exception as e:
            return {
                'child_id': item['child_id'],
                'child_name': item['child_name'],
                'parent_ids': item['parent_ids'],
                'parent_names': item['parent_names'],
                'selected': None,
                'error': str(e)
            }

    # Process all multi-parent nodes in parallel
    results = run_parallel(
        items_to_process,
        pick_best_parent,
        max_concurrent=MAX_CONCURRENT_FLASH,
        desc="Multi-parent resolution"
    )

    # Apply results
    for res in results:
        if isinstance(res, Exception):
            result.add_error(f"Resolution failed: {res}")
            continue

        if res.get('error'):
            result.add_warning(f"Error for '{res['child_name']}': {res['error']}")
            # Fallback: pick first parent
            res['selected'] = res['parent_names'][0] if res['parent_names'] else None

        child_id = res['child_id']
        child_name = res['child_name']
        parent_names = res['parent_names']
        selected_name = res['selected']

        if not selected_name or selected_name not in parent_names:
            result.add_warning(f"LLM returned invalid parent '{selected_name}' for '{child_name}'")
            selected_name = parent_names[0] if parent_names else None

        if not selected_name:
            continue

        # Find selected parent
        selected_parent = Pathway.query.filter_by(name=selected_name).first()
        if not selected_parent:
            continue

        # Verify this won't create a cycle
        test_graph = dict(parent_graph)
        test_graph[child_id] = [selected_parent.id]

        if would_create_cycle(child_id, selected_parent.id, test_graph):
            result.add_warning(f"Selecting '{selected_name}' for '{child_name}' would create cycle. Trying alternative...")
            # Try other parents
            for alt_name in parent_names:
                if alt_name != selected_name:
                    alt_parent = Pathway.query.filter_by(name=alt_name).first()
                    if alt_parent:
                        test_graph[child_id] = [alt_parent.id]
                        if not would_create_cycle(child_id, alt_parent.id, test_graph):
                            selected_parent = alt_parent
                            selected_name = alt_name
                            break
            else:
                result.add_error(f"No valid parent found for '{child_name}' - all create cycles")
                continue

        if dry_run:
            logger.info(f"[DRY RUN] SELECT PARENT: '{selected_name}' for '{child_name}'")
            result.add_change(Change(
                change_type=ChangeType.REPARENT,
                entity_type='pathway',
                entity_id=child_id,
                old_value=parent_names,
                new_value=selected_name,
                reason="Tree enforcement"
            ))
            continue

        # Delete other parent links
        for link in PathwayParent.query.filter_by(child_pathway_id=child_id).all():
            if link.parent_pathway_id != selected_parent.id:
                db.session.delete(link)

        db.session.commit()

        result.add_change(Change(
            change_type=ChangeType.REPARENT,
            entity_type='pathway',
            entity_id=child_id,
            old_value=parent_names,
            new_value=selected_name,
            reason="Tree enforcement"
        ))

        logger.info(f"Enforced parent '{selected_name}' for '{child_name}'")

    # Recalculate levels after tree changes
    if not dry_run and result.changes:
        recalculate_all_levels(db, Pathway, PathwayParent)

    # Verify no cycles remain
    parent_graph = build_parent_graph(PathwayParent)
    cycles = find_all_cycles(parent_graph)
    if cycles:
        result.add_error(f"CRITICAL: {len(cycles)} cycles detected after tree enforcement!")
        result.success = False

    # Verify no multi-parent nodes remain
    multi = detect_multi_parent_nodes(parent_graph)
    if multi:
        result.add_warning(f"{len(multi)} pathways still have multiple parents")

    return result
```

### Step 3: Verify syntax

Run: `cd "/Users/aryanzandi/Documents/5008 Backup Avoid Messy [Older Than 8] Retry [11-14]/5008 copy 11 [WORKING VERSION 1]" && python3 -m py_compile scripts/pathway_v2/step6_reorganize_pathways.py && echo "Syntax OK"`

Expected: "Syntax OK"

### Step 4: Commit

```bash
git add scripts/pathway_v2/step6_reorganize_pathways.py
git commit -m "perf: parallelize Step 6 phases 1-2

- Phase 1: Parallel deduplication batch processing
- Phase 2: Parallel multi-parent resolution
- Expected: ~10 min -> ~1-2 minutes"
```

---

## Task 7: Optimize Evidence Validator

**Files:**
- Modify: `utils/evidence_validator.py`

### Step 1: Add parallel batch processing

Find the main validation function in `utils/evidence_validator.py` and add parallel processing. Add near the top (after imports):

```python
# Add to imports section
import asyncio
from concurrent.futures import ThreadPoolExecutor

# Add after existing constants
MAX_CONCURRENT_PRO = 4  # Conservative for gemini-3.1-pro-preview
```

Then find the function that processes batches sequentially and wrap it with parallel execution. Add this helper function:

```python
def validate_evidence_parallel(
    main_protein: str,
    interactors: List[Dict[str, Any]],
    api_key: str,
    batch_size: int = 5,
    verbose: bool = False
) -> List[Dict[str, Any]]:
    """
    Validate all interactors in parallel batches.

    OPTIMIZED: All batches run simultaneously instead of sequentially.
    Expected speedup: ~12 min -> ~3-4 minutes
    """
    if not interactors:
        return []

    # Split into batches
    batches = [interactors[i:i + batch_size] for i in range(0, len(interactors), batch_size)]
    total = len(interactors)

    print(f"[INFO] Validating {total} interactors in {len(batches)} parallel batches")

    def validate_batch(batch_data):
        """Validate a single batch."""
        batch_idx, batch = batch_data
        batch_start = batch_idx * batch_size
        batch_end = min(batch_start + len(batch), total)

        try:
            prompt = create_validation_prompt(
                main_protein, batch, batch_start, batch_end, total
            )
            response = call_gemini_validation(prompt, api_key, verbose)
            result = extract_json_from_response(response)
            return {
                'batch_idx': batch_idx,
                'interactors': result.get('interactors', []),
                'error': None
            }
        except Exception as e:
            print(f"[WARN] Batch {batch_idx + 1} failed: {e}")
            return {
                'batch_idx': batch_idx,
                'interactors': batch,  # Return original on failure
                'error': str(e)
            }

    # Run all batches in parallel with limited concurrency
    with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_PRO) as executor:
        batch_data = list(enumerate(batches))
        results = list(executor.map(validate_batch, batch_data))

    # Sort by batch index and flatten
    results.sort(key=lambda x: x['batch_idx'])

    validated = []
    errors = 0
    for r in results:
        if r.get('error'):
            errors += 1
        validated.extend(r.get('interactors', []))

    print(f"[INFO] Validation complete. {len(validated)} interactors processed, {errors} batch errors")

    return validated
```

### Step 2: Verify syntax

Run: `cd "/Users/aryanzandi/Documents/5008 Backup Avoid Messy [Older Than 8] Retry [11-14]/5008 copy 11 [WORKING VERSION 1]" && python3 -m py_compile utils/evidence_validator.py && echo "Syntax OK"`

Expected: "Syntax OK"

### Step 3: Commit

```bash
git add utils/evidence_validator.py
git commit -m "perf: parallelize evidence validator batches

- All validation batches run simultaneously
- Conservative concurrency (4) for gemini-3.1-pro-preview
- Expected: ~12 min -> ~3-4 minutes"
```

---

## Task 8: Integration Test

**Files:**
- Test: Manual test run

### Step 1: Verify all imports work together

Run:
```bash
cd "/Users/aryanzandi/Documents/5008 Backup Avoid Messy [Older Than 8] Retry [11-14]/5008 copy 11 [WORKING VERSION 1]"
python3 -c "
from scripts.pathway_v2.async_utils import run_parallel, MAX_CONCURRENT_FLASH
from scripts.pathway_v2.cache import get_pathway_cache, save_cache
from scripts.pathway_v2.llm_utils import _call_gemini_json_cached
print('All imports OK')
print(f'Max concurrent: {MAX_CONCURRENT_FLASH}')
print(f'Cache stats: {get_pathway_cache().stats()}')
"
```

Expected: "All imports OK" with stats

### Step 2: Run unit tests

Run:
```bash
cd "/Users/aryanzandi/Documents/5008 Backup Avoid Messy [Older Than 8] Retry [11-14]/5008 copy 11 [WORKING VERSION 1]"
python tests/test_async_utils.py
python tests/test_cache.py
```

Expected: Both show "All tests passed!"

### Step 3: Final commit

```bash
git add -A
git commit -m "feat: complete pipeline performance optimization

Summary:
- Added async_utils.py for parallel LLM execution
- Added cache.py for avoiding redundant API calls
- Parallelized Step 4 (hierarchy building): ~25 min -> ~2-3 min
- Parallelized Step 5 (sibling discovery): ~15 min -> ~30-45 sec
- Parallelized Step 6 phases 1-2: ~10 min -> ~1-2 min
- Parallelized evidence validator: ~12 min -> ~3-4 min

Expected total improvement: ~60 min -> ~8-10 min (6-8x speedup)"
```

---

## Verification Checklist

After implementation, verify:

- [ ] `python tests/test_async_utils.py` passes
- [ ] `python tests/test_cache.py` passes
- [ ] All step files have valid syntax (`python3 -m py_compile`)
- [ ] Cache file is created at `cache/pathway_hierarchy_cache.json` after first run
- [ ] Pipeline completes in under 15 minutes
- [ ] Quality of pathway assignments is unchanged (spot check a few)

---

*Plan created: 2025-12-31*
