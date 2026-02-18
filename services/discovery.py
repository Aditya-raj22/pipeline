"""URL discovery service - direct probing + DuckDuckGo fallback."""
import asyncio
import httpx
from dataclasses import dataclass
from typing import Literal
from utils.search import search_ddg


@dataclass
class DiscoveredURL:
    """Classified pipeline URL."""
    url: str
    title: str
    snippet: str
    url_type: Literal["overview", "drug_specific", "news", "irrelevant"]
    relevance_score: float


def _guess_domain(company: str) -> str:
    clean = company.lower().replace(" ", "").replace(",", "").replace(".", "")
    if "pharmaceuticals" in clean:
        clean = clean.replace("pharmaceuticals", "pharma")
    return clean


async def _probe_url(client: httpx.AsyncClient, url: str) -> str | None:
    """HEAD request — returns final URL if 200, None otherwise."""
    try:
        resp = await client.head(url)
        if resp.status_code == 200:
            return str(resp.url)
    except Exception:
        pass
    return None


async def _probe_common_urls(company: str) -> list[DiscoveredURL]:
    """Try common pipeline URL patterns with HEAD requests (~1s total)."""
    domain = _guess_domain(company)
    bases = [f"https://www.{domain}.com", f"https://{domain}.com"]
    suffixes = [
        "/pipeline", "/en/pipeline", "/pipeline.html", "/our-pipeline",
        "/research/pipeline", "/science/pipeline", "/rnd/pipeline",
        "/en/company/pipeline01", "/en/rnd/pipeline",
        "/pipeline/", "/products/pipeline",
    ]
    patterns = [f"{b}{s}" for b in bases for s in suffixes]
    headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}
    async with httpx.AsyncClient(timeout=5, follow_redirects=True, headers=headers) as client:
        results = await asyncio.gather(*[_probe_url(client, u) for u in patterns])

    seen = set()
    found = []
    for url in results:
        if url and url not in seen:
            seen.add(url)
            found.append(DiscoveredURL(
                url=url, title="Direct probe", snippet="",
                url_type="overview", relevance_score=1.0,
            ))
    return found


def _classify(url: str, title: str, snippet: str, company: str) -> tuple[str, float]:
    """Fast heuristic classification — no LLM needed."""
    url_lower = url.lower()
    title_lower = title.lower()
    snippet_lower = snippet.lower()
    company_slug = _guess_domain(company)

    # Company's own pipeline page = best
    is_company_site = company_slug in url_lower.replace(".", "").replace("-", "")

    # Deprioritize news/press pages even on company site
    is_news_path = any(p in url_lower for p in ["/news", "/press", "/media", "news_view", "/hire", "/career", "/recruit"])

    # pipeline01 or /pipeline (not pipeline02, pipeline03 etc which are drug-specific)
    import re
    is_overview_url = bool(re.search(r'(/pipeline01|/pipeline\.html|/pipeline/?$|/rnd|/r-d)', url_lower))
    is_drug_pipeline = bool(re.search(r'/pipeline0[2-9]|/pipeline[1-9]\d', url_lower))

    if is_company_site and not is_news_path and is_overview_url:
        return ("overview", 1.0)

    if is_company_site and not is_news_path and is_drug_pipeline:
        return ("drug_specific", 0.8)

    if is_company_site and not is_news_path and "pipeline" in (title_lower + snippet_lower):
        return ("overview", 0.9)

    if is_company_site and not is_news_path and any(p in url_lower for p in ["/product", "/drug", "/candidate", "/program"]):
        return ("drug_specific", 0.7)

    if is_company_site and is_news_path:
        return ("news", 0.4)

    if is_company_site:
        return ("drug_specific", 0.5)

    # Third-party databases
    if any(db in url_lower for db in ["patsnap", "cortellis", "evaluate", "adisinsight"]):
        return ("overview", 0.6)

    # News
    if any(n in url_lower for n in ["news", "press", "fiercebiotech", "biospace", "reuters"]):
        return ("news", 0.4)

    if "pipeline" in snippet_lower:
        return ("overview", 0.5)

    return ("irrelevant", 0.2)


async def discover_pipeline_urls(company: str) -> list[DiscoveredURL]:
    """Discover pipeline URLs via direct probing + DDG fallback."""
    # Fast path: try common URL patterns (~1s)
    probed = await _probe_common_urls(company)
    if probed:
        print(f"  Direct probe hit: {probed[0].url}")
        return probed

    # Fallback: DDG search
    domain = f"{_guess_domain(company)}.com"
    queries = [
        f"site:{domain} pipeline",
        f"{company} pipeline",
        f"{company} drug candidates clinical trials",
    ]

    results_lists = await asyncio.gather(*[search_ddg(q, max_results=10) for q in queries])

    # Dedupe by URL
    seen = set()
    all_results = []
    for results in results_lists:
        for r in results:
            if r.url not in seen:
                seen.add(r.url)
                all_results.append(r)

    # Classify and build DiscoveredURL objects
    discovered = []
    for r in all_results:
        url_type, score = _classify(r.url, r.title, r.snippet, company)
        discovered.append(DiscoveredURL(
            url=r.url, title=r.title, snippet=r.snippet,
            url_type=url_type, relevance_score=score,
        ))

    # Sort: overview first, then by relevance
    type_priority = {"overview": 0, "drug_specific": 1, "news": 2, "irrelevant": 3}
    discovered.sort(key=lambda x: (type_priority.get(x.url_type, 3), -x.relevance_score))

    return discovered
