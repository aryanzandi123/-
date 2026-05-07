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
