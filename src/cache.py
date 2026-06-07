"""
cache.py — Disk-backed computation cache with TTL and surgical invalidation.

Why this exists: SEC XBRL facts barely change (a company files quarterly), so
recomputing derived metrics on every session — or worse, every UI interaction — is
wasteful. This caches *computed* results to disk so:
  - a cold start is fast (no recompute from a fresh process),
  - the UI layer doesn't need to know how caching works,
  - invalidation is surgical (drop one company, not the whole cache),
  - entries expire after a TTL so data refreshes on its own cadence.

Used by metrics.py / briefing.py / linkage.py / conflicts.py. The Streamlit app
sits on top and stays cache-agnostic.

Design: keyed JSON blobs under data/compute_cache/, with an embedded timestamp.
Values must be JSON-serializable (we store dataclasses via asdict()).
"""
from __future__ import annotations

import json
import time
import hashlib
from pathlib import Path
from typing import Any, Callable, Optional

CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "compute_cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_TTL = 7 * 24 * 3600  # 7 days — filings update at most quarterly


def _path(namespace: str, key: str) -> Path:
    safe = hashlib.sha1(f"{namespace}:{key}".encode()).hexdigest()[:16]
    return CACHE_DIR / f"{namespace}_{key}_{safe}.json"


def get(namespace: str, key: str, ttl: int = DEFAULT_TTL) -> Optional[Any]:
    """Return a cached value if present and not expired, else None."""
    p = _path(namespace, key)
    if not p.exists():
        return None
    try:
        blob = json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    if time.time() - blob.get("_ts", 0) > ttl:
        return None  # expired; treated as a miss (refetch)
    return blob.get("value")


def put(namespace: str, key: str, value: Any) -> None:
    p = _path(namespace, key)
    try:
        p.write_text(json.dumps({"_ts": time.time(), "value": value}))
    except (OSError, TypeError):
        pass  # caching is best-effort; never break the caller


def memoize(namespace: str, key: str, producer: Callable[[], Any],
            ttl: int = DEFAULT_TTL) -> Any:
    """get-or-compute: return cached value or run producer(), cache, return it."""
    hit = get(namespace, key, ttl=ttl)
    if hit is not None:
        return hit
    value = producer()
    put(namespace, key, value)
    return value


def invalidate(namespace: str, key: Optional[str] = None) -> int:
    """
    Surgical invalidation. With a key, drop just that entry; without, drop the
    whole namespace. Returns how many files were removed. This is what lets adding
    one company recompute only that company instead of nuking everything.
    """
    removed = 0
    if key is not None:
        p = _path(namespace, key)
        if p.exists():
            p.unlink()
            removed += 1
        return removed
    for f in CACHE_DIR.glob(f"{namespace}_*.json"):
        f.unlink()
        removed += 1
    return removed


def clear_all() -> int:
    removed = 0
    for f in CACHE_DIR.glob("*.json"):
        f.unlink()
        removed += 1
    return removed
