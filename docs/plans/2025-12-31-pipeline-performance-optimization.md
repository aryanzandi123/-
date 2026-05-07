# Pipeline Performance Optimization Design

**Date:** 2025-12-31
**Goal:** Reduce pipeline runtime from ~60 minutes to under 15 minutes
**Approach:** Parallel API calls with semaphore-based rate limiting + simple caching

---

## 1. Problem Statement

The pathway pipeline processes ~62 interactions and takes approximately 1 hour due to sequential API calls with artificial delays.

### Current Bottlenecks

| Step | Current Time | Issue | API Calls |
|------|-------------|-------|-----------|
| Step 4 (Hierarchy) | ~25 min | Sequential parent lookups + 0.5s sleep | ~100+ |
| Step 5 (Siblings) | ~15 min | Sequential sibling discovery + 1s sleep | ~25 |
| Step 6 (Reorganize) | ~10 min | Sequential phases + 0.3-0.5s sleeps | ~30-50 |
| Evidence Validator | ~12 min | Sequential batch processing | ~10-15 |
| **Total** | **~62 min** | | **~200 calls** |

### Rate Limits (gemini-3-flash-preview)

- 1000 RPM (requests per minute)
- 1M TPM (tokens per minute)
- 10,000 RPD (requests per day)

With 1000 RPM, we can safely run 15-20 concurrent requests.

---

## 2. Solution Architecture

### Core Pattern: Async with Semaphore

```python
import asyncio
from concurrent.futures import ThreadPoolExecutor

MAX_CONCURRENT = 15  # Safe with 1000 RPM limit

async def parallel_llm_calls(items, call_fn):
    """Run LLM calls in parallel with rate limiting."""
    semaphore = asyncio.Semaphore(MAX_CONCURRENT)

    async def bounded_call(item):
        async with semaphore:
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(None, call_fn, item)

    tasks = [bounded_call(item) for item in items]
    return await asyncio.gather(*tasks, return_exceptions=True)
```

### Simple Caching Layer

```python
class PathwayCache:
    """Cache for parent-child relationships to avoid redundant LLM calls."""

    def get_parent(self, child_name: str) -> str | None
    def set_parent(self, child_name: str, parent_name: str)
    def get_siblings(self, parent_name: str) -> list | None
    def set_siblings(self, parent_name: str, siblings: list)
    def save_to_disk(self)  # Persist to cache/pathway_hierarchy_cache.json
```

---

## 3. Step-by-Step Optimizations

### 3.1 Step 4: Hierarchy Building

**Current:** Sequential chain walking (one parent lookup at a time)

**New:** Two-phase parallel climbing

```
Phase A: All leaf pathways ask for parent SIMULTANEOUSLY
         [Aggrephagy, mTOR, p53, ...] -> parallel API calls

Phase B: Dedupe results, process next level in parallel
         Repeat until all chains reach roots
```

**Key insight:** Each "level" of hierarchy is processed in parallel. Only serialize between levels where dependencies exist.

**Expected:** ~25 min → ~2-3 min

### 3.2 Step 5: Sibling Discovery

**Current:** One parent at a time with 1s sleep

**New:** Fully parallel (all parents independent)

```python
results = await parallel_llm_calls(parent_ids, discover_siblings_for_parent)
```

**Expected:** ~15 min → ~30-45 sec

### 3.3 Step 6: Reorganization

**Current:** 6 sequential phases with sleeps

**New:** Parallelize LLM-heavy phases (1-3)

| Phase | Parallelizable | Strategy |
|-------|---------------|----------|
| Phase 1: Deduplication | Yes | Parallel merge decisions |
| Phase 2: Tree Enforcement | Yes | Parallel multi-parent resolution |
| Phase 3: Hierarchy Repair | Partial | Pass A parallel, Pass B careful |
| Phase 4-6 | N/A | No LLM calls (already fast) |

**Expected:** ~10 min → ~1-2 min

### 3.4 Evidence Validator

**Current:** Sequential batch processing

**New:** Parallel batches with separate semaphore (Pro model may have lower limits)

```python
EVIDENCE_SEMAPHORE = asyncio.Semaphore(4)  # Conservative for gemini-3.1-pro-preview
```

**Expected:** ~12 min → ~3-4 min

---

## 4. Expected Results

| Step | Current | Optimized | Improvement |
|------|---------|-----------|-------------|
| Step 4 | ~25 min | ~2-3 min | 8-10x |
| Step 5 | ~15 min | ~30-45 sec | 20-30x |
| Step 6 | ~10 min | ~1-2 min | 5-8x |
| Evidence | ~12 min | ~3-4 min | 3-4x |
| **Total** | **~62 min** | **~8-10 min** | **6-8x** |

---

## 5. File Changes

### New Files

```
scripts/pathway_v2/
├── async_utils.py      # Parallel execution helpers, semaphore management
├── cache.py            # PathwayCache class with disk persistence
```

### Modified Files

```
scripts/pathway_v2/
├── step4_build_hierarchy_backwards.py  # Two-phase parallel climbing
├── step5_discover_siblings.py          # Fully parallel
├── step6_reorganize_pathways.py        # Parallel phases 1-3
├── llm_utils.py                        # Add async wrapper function

utils/
├── evidence_validator.py               # Parallel batch processing
```

---

## 6. Implementation Order

1. **async_utils.py** - Core parallel infrastructure
2. **cache.py** - Simple caching layer
3. **llm_utils.py** - Add `_call_gemini_json_async()` wrapper
4. **step5** - Easiest win (fully independent calls)
5. **step4** - Two-phase parallel climbing
6. **step6** - Parallel phases 1-3
7. **evidence_validator** - Parallel batches
8. **Testing** - Verify quality unchanged, measure speedup

---

## 7. Risk Mitigation

### Quality Preservation
- All prompts remain identical
- Only execution order changes (parallel vs sequential)
- Cache only stores LLM responses, doesn't modify them

### Error Handling
- `return_exceptions=True` prevents one failure from killing all tasks
- Failed items logged and can retry individually
- Graceful degradation to sequential if async fails

### Rate Limiting
- Semaphore enforces max concurrent requests
- Well under 1000 RPM limit (max ~15 concurrent)
- Separate semaphore for Pro model if needed

---

## 8. Constraints

1. **Quality CANNOT decrease** - hierarchies must be equally accurate
2. **Order matters for dependencies** - only parallelize truly independent operations
3. **Error handling** - graceful failure, don't crash pipeline
4. **Backwards compatible** - can disable parallelization if issues arise

---

*Design approved: 2025-12-31*
