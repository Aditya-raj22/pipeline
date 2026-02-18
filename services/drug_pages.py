"""Drug page enrichment - DDG search per asset, fetch HTML, LLM parse to fill gaps."""
import asyncio
import json
import httpx
from openai import AsyncOpenAI
from config import config
from utils.search import search_ddg
from utils.fetch import clean_html, fetch_content

client = AsyncOpenAI(api_key=config.openai_api_key)

DDG_SEMAPHORE = asyncio.Semaphore(5)
MAX_URLS_PER_ASSET = 3

GENERIC_VALUES = {
    "", "undisclosed", "unknown", "tbd", "n/a",
    "solid tumor", "solid tumors", "solid cancer", "cancer",
    "solid & blood tumor", "blood cancer", "hematologic cancer",
    "various solid tumors", "advanced solid tumors",
}

FILL_PROMPT = """Extract drug development information from this webpage.

Drug: {name}
Company: {company}

Current known data:
{current_data}

Webpage text:
{text}

Return JSON with ONLY fields you can confidently extract from the text:
{{
  "indication": "the DISEASE(S) being treated, semicolon-separated if multiple",
  "therapeutic_target": "molecular target(s)",
  "phase": "Phase 1, Phase 1/2, Phase 2, Phase 3, Preclinical, IND-enabling, etc.",
  "modality": "e.g., Bispecific Antibody, Small molecule, ADC, CAR-T",
  "therapeutic_area": "e.g., Oncology, Neurology, Immunology",
  "description": "1-sentence mechanism of action"
}}

Rules:
- "indication" must be a DISEASE or CONDITION (e.g., "NSCLC", "AML", "Parkinson's disease")
  NOT a treatment/regimen (e.g., NOT "combination with nivolumab"), NOT a target, NOT a modality
- Be SPECIFIC: "Non-small cell lung cancer (NSCLC)" not "Solid Tumor"
- Only fill fields where the text has clear evidence
- Use "" for fields you cannot confidently determine
- Do NOT repeat existing known data verbatim
- Return ONLY valid JSON, no explanation"""


def _is_generic(value: str) -> bool:
    if not value:
        return True
    # Handle semicolon/comma-separated values — generic if ALL parts are generic
    parts = [p.strip().lower() for p in value.replace(";", "/").replace(",", "/").split("/")]
    return all(p in GENERIC_VALUES for p in parts)


def _needs_enrichment(asset: dict) -> bool:
    """Check if asset has ANY gap worth filling — more aggressive than before."""
    # Always enrich if indication or description is generic
    if _is_generic(asset.get("Indication", "")):
        return True
    if _is_generic(asset.get("Description", "")):
        return True
    # Enrich if target is missing
    if _is_generic(asset.get("Therapeutic Target", "")):
        return True
    # Enrich if phase looks non-standard or generic
    phase = asset.get("Phase", "").strip().lower()
    if not phase or phase in ("undisclosed", "unknown", "tbd", "n/a"):
        return True
    return False


def _rank_urls(results: list, company: str) -> list[str]:
    """Rank and return top URLs from search results, best first."""
    company_lower = company.lower().replace(" ", "")
    buckets = {"company": [], "trials": [], "database": [], "other": []}

    for r in results:
        url_lower = r.url.lower()
        if company_lower in url_lower.replace(".", "").replace("-", ""):
            # Skip news/press pages on company site
            if any(p in url_lower for p in ["/news", "/press", "news_view"]):
                buckets["other"].append(r.url)
            else:
                buckets["company"].append(r.url)
        elif "clinicaltrials.gov" in url_lower:
            buckets["trials"].append(r.url)
        elif any(db in url_lower for db in ["drugbank", "adisinsight", "drugs.com"]):
            buckets["database"].append(r.url)
        else:
            buckets["other"].append(r.url)

    ranked = buckets["company"] + buckets["trials"] + buckets["database"] + buckets["other"]
    return ranked[:MAX_URLS_PER_ASSET]


async def _fetch_page_text(url: str) -> str:
    """Fetch HTML and extract text. httpx first, playwright fallback for thin content."""
    headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as c:
            resp = await c.get(url, headers=headers)
            if resp.status_code == 200 and "text/html" in resp.headers.get("content-type", ""):
                text, _ = clean_html(resp.text)
                if len(text) > 200:
                    return text[:8000]
    except Exception:
        pass

    # Playwright fallback for JS-rendered pages (short timeout)
    try:
        result = await asyncio.wait_for(fetch_content(url), timeout=15)
        if result.text and len(result.text) > 200:
            return result.text[:8000]
    except (asyncio.TimeoutError, Exception):
        pass  # silently skip — httpx covers most cases

    return ""


async def _parse_drug_page(text: str, name: str, company: str, asset: dict) -> dict:
    """LLM parse page text to fill schema gaps."""
    current = {k: v for k, v in asset.items() if v and v != "Undisclosed"}

    prompt = FILL_PROMPT.format(
        name=name,
        company=company,
        current_data=json.dumps(current, indent=2),
        text=text,
    )

    try:
        resp = await client.chat.completions.create(
            model=config.text_model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
        )
        content = resp.choices[0].message.content.strip()
        if content.startswith("```"):
            content = content.split("```")[1]
            if content.startswith("json"):
                content = content[4:]
        return json.loads(content)
    except Exception:
        return {}


_FIELD_MAP = {
    "indication": "Indication",
    "therapeutic_target": "Therapeutic Target",
    "phase": "Phase",
    "modality": "Modality",
    "therapeutic_area": "Therapeutic Area",
    "description": "Description",
}


def _match_overview_links(name: str, overview_links: list[str]) -> list[str]:
    """Find overview links that likely correspond to this asset."""
    if not overview_links or not name:
        return []
    slug = name.lower().replace("-", "").replace("_", "").replace(" ", "")
    return [l for l in overview_links if slug in l.lower().replace("-", "").replace("_", "")]


def _apply_updates(asset: dict, updates: dict) -> tuple[dict, bool]:
    """Apply LLM updates to asset, filling only generic gaps. Returns (merged, changed)."""
    merged = {**asset}
    changed = False
    for llm_key, schema_key in _FIELD_MAP.items():
        val = updates.get(llm_key, "")
        if val and _is_generic(merged.get(schema_key, "")):
            merged[schema_key] = val
            changed = True
    return merged, changed


async def enrich_one_asset(asset: dict, company: str, overview_links: list[str] = None) -> dict:
    """Enrich a single asset: snippets first, then page fetch if still needed."""
    async with DDG_SEMAPHORE:
        name = asset.get("Asset Name", "")
        if not name or name == "Undisclosed" or not _needs_enrichment(asset):
            return asset

        # DDG search
        results = await search_ddg(f'"{name}" "{company}"', max_results=8)
        if not results:
            results = await search_ddg(f'{name} {company} drug clinical trial', max_results=8)
        if not results and not overview_links:
            return asset

        # Step 1: Try snippet-based enrichment (zero fetch cost)
        working = {**asset}
        if results:
            snippets = "\n".join(f"- {r.title}: {r.snippet}" for r in results if r.snippet)
            if snippets and len(snippets) > 80:
                updates = await _parse_drug_page(snippets, name, company, working)
                working, _ = _apply_updates(working, updates)
                if not _needs_enrichment(working):
                    # Snippets filled all gaps — skip page fetching
                    return working

        # Step 2: Full page fetch for remaining gaps
        urls = _rank_urls(results, company) if results else []

        # Prepend matching overview links (highest priority — same domain)
        for link in reversed(_match_overview_links(name, overview_links)):
            if link not in urls:
                urls.insert(0, link)
        urls = urls[:MAX_URLS_PER_ASSET]

        if not urls:
            return working

        texts = await asyncio.gather(*[_fetch_page_text(u) for u in urls])

        combined = ""
        sources = []
        for url, text in zip(urls, texts):
            if text and len(text) > 100:
                combined += f"\n--- Source: {url} ---\n{text}\n"
                sources.append(url)
                if len(combined) > 12000:
                    break

        if not combined:
            return working

        updates = await _parse_drug_page(combined, name, company, working)
        merged, _ = _apply_updates(working, updates)

        # Track sources
        src = merged.get("Sources", "")
        new_sources = "; ".join(sources)
        if not src or src == "Undisclosed":
            merged["Sources"] = new_sources
        else:
            merged["Sources"] = f"{src}; {new_sources}"

        return merged


async def enrich_from_drug_pages(
    assets: list[dict],
    company: str,
    on_progress=None,
    overview_links: list[str] = None,
) -> list[dict]:
    """Enrich all assets via snippets + DDG page fetch + overview links."""
    if on_progress:
        await on_progress(f"[{company}] Enriching {len(assets)} assets from drug pages...")

    tasks = [enrich_one_asset(a, company, overview_links=overview_links) for a in assets]
    enriched = await asyncio.gather(*tasks)

    filled = sum(1 for old, new in zip(assets, enriched) if old != new)
    if on_progress:
        await on_progress(f"[{company}] Enriched {filled}/{len(assets)} assets with new data")

    return list(enriched)
