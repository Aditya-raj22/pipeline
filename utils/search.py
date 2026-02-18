"""DuckDuckGo search utility - replaces Serper (free, no API key)."""
import asyncio
from dataclasses import dataclass
from ddgs import DDGS


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str


def _search_sync(query: str, max_results: int = 5) -> list[dict]:
    return list(DDGS().text(query, max_results=max_results))


async def search_ddg(query: str, max_results: int = 5) -> list[SearchResult]:
    """Async DDG search. Returns list of SearchResult."""
    try:
        raw = await asyncio.to_thread(_search_sync, query, max_results)
        return [
            SearchResult(title=r.get("title", ""), url=r.get("href", ""), snippet=r.get("body", ""))
            for r in raw
        ]
    except Exception:
        return []
