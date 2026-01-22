"""Serper API client for web search."""
import asyncio
from dataclasses import dataclass
import httpx
from config import config

SERPER_URL = "https://google.serper.dev/search"


@dataclass
class SerperResult:
    """Single search result from Serper."""
    title: str
    link: str
    snippet: str
    position: int

    @classmethod
    def from_dict(cls, data: dict, position: int) -> "SerperResult":
        return cls(
            title=data.get("title", ""),
            link=data.get("link", ""),
            snippet=data.get("snippet", ""),
            position=position,
        )


async def search_serper(
    query: str,
    num_results: int = 10,
    gl: str = "us",
) -> list[SerperResult]:
    """
    Search using Serper API.

    Args:
        query: Search query string
        num_results: Number of results (max 100)
        gl: Country code for results

    Returns:
        List of SerperResult objects
    """
    if not config.serper_api_key:
        raise ValueError("SERPER_API_KEY not set in environment")

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            SERPER_URL,
            headers={
                "X-API-KEY": config.serper_api_key,
                "Content-Type": "application/json",
            },
            json={
                "q": query,
                "num": num_results,
                "gl": gl,
            },
        )
        resp.raise_for_status()
        data = resp.json()

    organic = data.get("organic", [])
    return [SerperResult.from_dict(r, i) for i, r in enumerate(organic)]


async def search_pipeline_urls(company: str) -> list[SerperResult]:
    """
    Search for pipeline URLs for a company.

    Runs two queries and deduplicates:
    1. "{company}" pipeline site:{domain}
    2. "{company}" drug candidates clinical trials
    """
    queries = [
        f'"{company}" pharmaceutical pipeline',
        f'"{company}" drug candidates clinical trials pipeline',
    ]

    tasks = [search_serper(q, num_results=5) for q in queries]
    results_lists = await asyncio.gather(*tasks, return_exceptions=True)

    # Flatten and dedupe by URL
    seen_urls = set()
    all_results = []

    for results in results_lists:
        if isinstance(results, Exception):
            continue
        for r in results:
            if r.link not in seen_urls:
                seen_urls.add(r.link)
                all_results.append(r)

    return all_results


# Convenience sync wrapper for testing
def search_serper_sync(query: str, num_results: int = 10) -> list[SerperResult]:
    """Synchronous wrapper for search_serper."""
    return asyncio.run(search_serper(query, num_results))
