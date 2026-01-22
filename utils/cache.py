"""Caching utilities for fetch results."""
import json
import time
import hashlib
from pathlib import Path
from typing import Optional
from config import config


def cache_key(url: str) -> str:
    """Generate cache key from URL."""
    return hashlib.md5(url.encode()).hexdigest()


def get_cached(url: str) -> Optional[dict]:
    """
    Get cached content for URL.

    Returns dict with 'content', 'type', 'ts' or None if not cached/expired.
    """
    cache_dir = Path(config.cache_dir)
    cache_dir.mkdir(exist_ok=True)

    path = cache_dir / f"{cache_key(url)}.json"
    if path.exists():
        data = json.loads(path.read_text())
        if time.time() - data["ts"] < config.cache_ttl:
            return data
    return None


def set_cache(url: str, content: str, content_type: str = "text") -> None:
    """Cache content for URL."""
    cache_dir = Path(config.cache_dir)
    cache_dir.mkdir(exist_ok=True)

    path = cache_dir / f"{cache_key(url)}.json"
    path.write_text(json.dumps({
        "ts": time.time(),
        "content": content,
        "type": content_type,
    }))


def clear_cache(url: str = None) -> int:
    """Clear cache for URL or all if url is None. Returns count cleared."""
    cache_dir = Path(config.cache_dir)
    if not cache_dir.exists():
        return 0

    count = 0
    if url:
        path = cache_dir / f"{cache_key(url)}.json"
        if path.exists():
            path.unlink()
            count = 1
    else:
        for path in cache_dir.glob("*.json"):
            path.unlink()
            count += 1
    return count
