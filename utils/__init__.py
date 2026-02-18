"""Utility modules."""
from utils.cache import get_cached, set_cache, clear_cache
from utils.fetch import fetch_content, FetchResult, close_browser

__all__ = [
    "get_cached", "set_cache", "clear_cache",
    "fetch_content", "FetchResult", "close_browser",
]
