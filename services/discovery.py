"""URL discovery service - finds pipeline pages for a company."""
import asyncio
import json
from dataclasses import dataclass
from typing import Literal
from openai import AsyncOpenAI
from config import config
from utils.serper import search_serper, SerperResult

client = AsyncOpenAI(api_key=config.openai_api_key)


@dataclass
class DiscoveredURL:
    """Classified pipeline URL."""
    url: str
    title: str
    snippet: str
    url_type: Literal["overview", "drug_specific", "news", "irrelevant"]
    relevance_score: float  # 0-1


CLASSIFY_PROMPT = """Classify these search results for finding pharmaceutical pipeline information.

Company: {company}

Results:
{results}

For each URL, classify as:
- "overview": Main pipeline page listing MULTIPLE drugs/assets (highest priority)
- "drug_specific": Page about a SINGLE drug/asset (useful for details)
- "news": News article about pipeline or company
- "irrelevant": Careers, contact, investor relations, unrelated content

Return JSON array with same order as input:
[{{"url": "...", "type": "overview|drug_specific|news|irrelevant", "relevance": 0.0-1.0}}]

Relevance scoring:
- 1.0: Official company pipeline page
- 0.8: Official company page with pipeline data
- 0.6: Third-party database with pipeline info
- 0.4: News article with pipeline details
- 0.2: Tangentially related
- 0.0: Irrelevant

Return ONLY valid JSON array, no explanation."""


async def classify_urls(
    company: str,
    results: list[SerperResult],
) -> list[DiscoveredURL]:
    """Use LLM to classify search results by URL type."""
    if not results:
        return []

    # Format results for prompt
    results_text = "\n".join([
        f"{i+1}. {r.title}\n   URL: {r.link}\n   Snippet: {r.snippet}"
        for i, r in enumerate(results)
    ])

    prompt = CLASSIFY_PROMPT.format(company=company, results=results_text)

    try:
        resp = await client.chat.completions.create(
            model=config.text_model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
        )
        content = resp.choices[0].message.content.strip()

        # Parse JSON - handle markdown code blocks
        if content.startswith("```"):
            content = content.split("```")[1]
            if content.startswith("json"):
                content = content[4:]

        classifications = json.loads(content)
    except Exception as e:
        # Fallback: use heuristics if LLM fails
        print(f"LLM classification failed: {e}, using heuristics")
        classifications = _heuristic_classify(results)

    # Build DiscoveredURL objects
    discovered = []
    for i, r in enumerate(results):
        if i < len(classifications):
            c = classifications[i]
            discovered.append(DiscoveredURL(
                url=r.link,
                title=r.title,
                snippet=r.snippet,
                url_type=c.get("type", "irrelevant"),
                relevance_score=c.get("relevance", 0.5),
            ))
        else:
            # Missing classification, use heuristic
            url_type, score = _heuristic_single(r)
            discovered.append(DiscoveredURL(
                url=r.link,
                title=r.title,
                snippet=r.snippet,
                url_type=url_type,
                relevance_score=score,
            ))

    return discovered


def _heuristic_classify(results: list[SerperResult]) -> list[dict]:
    """Fallback heuristic classification."""
    return [
        {"url": r.link, "type": _heuristic_single(r)[0], "relevance": _heuristic_single(r)[1]}
        for r in results
    ]


def _heuristic_single(r: SerperResult) -> tuple[str, float]:
    """Heuristic classification for single result."""
    url_lower = r.link.lower()
    title_lower = r.title.lower()
    snippet_lower = r.snippet.lower()

    # Check for pipeline overview indicators
    if "pipeline" in url_lower and any(x in url_lower for x in ["/pipeline", "pipeline.html", "pipeline01"]):
        return ("overview", 0.9)

    if "pipeline" in title_lower and "overview" in title_lower:
        return ("overview", 0.85)

    # Check for drug-specific pages
    if any(x in url_lower for x in ["pipeline0", "product/", "drug/"]):
        return ("drug_specific", 0.7)

    # Check for news
    if any(x in url_lower for x in ["news", "press", "article", "fiercebiotech", "biospace"]):
        return ("news", 0.5)

    # Check for third-party databases
    if any(x in url_lower for x in ["patsnap", "cortellis", "evaluate"]):
        return ("drug_specific", 0.6)

    # Default
    if "pipeline" in snippet_lower:
        return ("overview", 0.6)

    return ("irrelevant", 0.3)


async def discover_pipeline_urls(
    company: str,
    max_results: int = 20,
) -> list[DiscoveredURL]:
    """
    Discover and classify pipeline URLs for a company.

    Returns URLs sorted by: overview first, then by relevance score.
    """
    # Run multiple search queries with different result counts
    # Site-specific query gets more results to find all drug pages
    queries_with_counts = [
        (f'"{company}" pipeline', 5),
        (f'"{company}" drug candidates clinical trials', 5),
        (f'site:{_guess_domain(company)} pipeline', 15),  # More results for site crawl
    ]

    # Execute searches in parallel
    tasks = [search_serper(q, num_results=n) for q, n in queries_with_counts]
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

    # Classify all URLs
    discovered = await classify_urls(company, all_results[:max_results])

    # Sort: overview first, prefer main domain, then by relevance
    type_priority = {"overview": 0, "drug_specific": 1, "news": 2, "irrelevant": 3}
    company_lower = company.lower().replace(" ", "")

    def sort_key(x):
        type_score = type_priority.get(x.url_type, 3)
        # Prefer URLs with www. (main domain) over regional variants
        is_main_domain = 1 if "www." in x.url else 0
        # Prefer .com over regional TLDs
        is_com = 1 if ".com" in x.url else 0
        # Deprioritize regional variants (.us, .eu, .uk, etc)
        is_regional = 1 if any(f".{r}" in x.url for r in ["us", "eu", "uk", "de", "jp", "kr"]) else 0
        return (type_score, -is_main_domain, -is_com, is_regional, -x.relevance_score)

    discovered.sort(key=sort_key)

    return discovered


def _guess_domain(company: str) -> str:
    """Guess company domain from name."""
    # Simple heuristic: lowercase, remove spaces, add .com
    clean = company.lower().replace(" ", "").replace(",", "").replace(".", "")
    # Handle common patterns
    if "pharmaceuticals" in clean:
        clean = clean.replace("pharmaceuticals", "pharma")
    return f"{clean}.com"


# Sync wrapper for testing
def discover_pipeline_urls_sync(company: str) -> list[DiscoveredURL]:
    """Synchronous wrapper for discover_pipeline_urls."""
    return asyncio.run(discover_pipeline_urls(company))
