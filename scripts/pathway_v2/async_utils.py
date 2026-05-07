#!/usr/bin/env python3
"""
Async Utilities for Parallel LLM Calls
======================================
Provides semaphore-controlled parallel execution for Gemini API calls.
"""

import atexit
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
        atexit.register(shutdown_executor)
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
