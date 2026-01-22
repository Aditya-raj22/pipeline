"""Enrichment service - enhances assets with web search data."""
import asyncio
import json
from dataclasses import dataclass
from openai import AsyncOpenAI
from config import config
from utils.serper import search_serper
from utils.fetch import fetch_content

client = AsyncOpenAI(api_key=config.openai_api_key)


@dataclass
class EnrichmentResult:
    """Result of enriching a single asset."""
    asset: dict  # Updated asset data
    sources: list[str]  # URLs used
    updated_fields: list[str]  # Fields that were updated
    confidence: str  # "high", "medium", "low"


ENRICHMENT_PROMPT = """You are updating pharmaceutical asset data with the latest web information.

Asset: {asset_name}
Company: {company}

Current data:
{current_data}

Latest web search results:
{search_results}

Instructions:
1. If web results contain NEWER information (phase advancement, new indication, trial results), update the field
2. If web results CONTRADICT current data with more recent info, update it
3. If web results ADD information for "Undisclosed" fields, fill them in
4. Keep current values if web results don't provide better/newer information
5. Add a "Latest News" field with a 1-sentence summary of the most significant recent update (if any)

Return JSON with these exact fields:
{{
  "Therapeutic Area": "...",
  "Modality": "...",
  "Phase": "...",
  "Asset Name": "...",
  "Description": "...",
  "Therapeutic Target": "...",
  "Indication": "...",
  "Company": "...",
  "Latest News": "..." or null if no significant news,
  "updated_fields": ["field1", "field2"] or [] if no changes,
  "confidence": "high" | "medium" | "low"
}}

Only update fields where you have CONFIDENT new information from the search results."""


async def enrich_from_snippets(
    asset: dict,
    company: str,
) -> EnrichmentResult:
    """
    Tier 1: Enrich asset using search snippets only.

    Fast and cheap - no additional page fetches.
    """
    asset_name = asset.get("Asset Name", "Unknown")

    # Build search query
    query = f'"{asset_name}" "{company}" clinical trial latest 2024 2025'

    try:
        results = await search_serper(query, num_results=config.max_enrichment_sources)
    except Exception as e:
        print(f"    Search failed for {asset_name}: {e}")
        return EnrichmentResult(
            asset=asset,
            sources=[],
            updated_fields=[],
            confidence="low"
        )

    if not results:
        return EnrichmentResult(
            asset=asset,
            sources=[],
            updated_fields=[],
            confidence="low"
        )

    # Format snippets for LLM
    search_context = "\n\n".join([
        f"[{r.title}]\nURL: {r.link}\n{r.snippet}"
        for r in results
    ])

    prompt = ENRICHMENT_PROMPT.format(
        asset_name=asset_name,
        company=company,
        current_data=json.dumps(asset, indent=2),
        search_results=search_context,
    )

    try:
        response = await client.chat.completions.create(
            model=config.text_model,
            messages=[
                {"role": "system", "content": "You update pharma asset data with web search results. Return valid JSON only."},
                {"role": "user", "content": prompt}
            ],
            response_format={"type": "json_object"}
        )

        result = json.loads(response.choices[0].message.content)

        # Extract metadata
        updated_fields = result.pop("updated_fields", [])
        confidence = result.pop("confidence", "medium")

        # Merge with original (keep original if new is empty/Undisclosed)
        for key, value in asset.items():
            if key not in result or not result[key] or result[key] == "Undisclosed":
                result[key] = value

        return EnrichmentResult(
            asset=result,
            sources=[r.link for r in results],
            updated_fields=updated_fields,
            confidence=confidence,
        )

    except Exception as e:
        print(f"    LLM enrichment failed for {asset_name}: {e}")
        return EnrichmentResult(
            asset=asset,
            sources=[r.link for r in results],
            updated_fields=[],
            confidence="low"
        )


async def deep_fetch_for_gaps(
    asset: dict,
    company: str,
    sources: list[str],
) -> dict:
    """
    Tier 2: Fetch full page content for assets with critical gaps.

    Only used when snippet enrichment leaves required fields as "Undisclosed".
    """
    # Check for critical gaps
    critical_fields = ["Phase", "Indication"]
    gaps = [f for f in critical_fields if asset.get(f) in [None, "", "Undisclosed"]]

    if not gaps:
        return asset

    asset_name = asset.get("Asset Name", "Unknown")
    print(f"    Deep fetch for {asset_name} (missing: {gaps})")

    # Try fetching from sources
    for url in sources[:2]:  # Limit to 2 pages
        try:
            content = await fetch_content(url)
            if content.method == "failed" or len(content.text) < 200:
                continue

            # Extract specific fields from full content
            prompt = f"""Extract these specific fields for {asset_name} ({company}) from this content:

Missing fields: {gaps}

Content:
{content.text[:15000]}

Return JSON with only the requested fields. Use null if not found.
Example: {{"Phase": "Phase 2", "Indication": "Solid tumors"}}"""

            response = await client.chat.completions.create(
                model=config.text_model,
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"}
            )

            extracted = json.loads(response.choices[0].message.content)

            # Update asset with extracted values
            for field, value in extracted.items():
                if value and value != "null" and asset.get(field) in [None, "", "Undisclosed"]:
                    asset[field] = value
                    print(f"      Found {field}: {value}")

            # Check if gaps are filled
            remaining_gaps = [f for f in gaps if asset.get(f) in [None, "", "Undisclosed"]]
            if not remaining_gaps:
                break

        except Exception as e:
            continue

    return asset


async def enrich_asset(
    asset: dict,
    company: str,
    deep_fetch: bool = True,
) -> EnrichmentResult:
    """
    Full enrichment pipeline for single asset.

    1. Snippet-based enrichment (Tier 1)
    2. Deep fetch if gaps remain (Tier 2, optional)
    """
    # Tier 1: Snippet enrichment
    result = await enrich_from_snippets(asset, company)

    # Tier 2: Deep fetch for gaps (if enabled)
    if deep_fetch and result.sources:
        result.asset = await deep_fetch_for_gaps(
            result.asset,
            company,
            result.sources,
        )

    return result


async def enrich_assets(
    assets: list[dict],
    company: str,
    deep_fetch: bool = True,  # Enabled - fetch full page for critical gaps
    max_concurrent: int = 3,
) -> list[EnrichmentResult]:
    """
    Enrich multiple assets with concurrency control.

    Args:
        assets: List of mapped assets
        company: Company name
        deep_fetch: Whether to do Tier 2 deep fetching
        max_concurrent: Max concurrent enrichment tasks

    Returns:
        List of EnrichmentResult objects
    """
    semaphore = asyncio.Semaphore(max_concurrent)

    async def enrich_with_limit(asset):
        async with semaphore:
            asset_name = asset.get("Asset Name", "Unknown")
            print(f"  Enriching: {asset_name}")
            return await enrich_asset(asset, company, deep_fetch)

    tasks = [enrich_with_limit(asset) for asset in assets]
    return await asyncio.gather(*tasks)


def merge_enrichment_results(results: list[EnrichmentResult]) -> tuple[list[dict], list[str]]:
    """
    Merge enrichment results into final asset list.

    Returns:
        (assets, all_sources)
    """
    assets = []
    all_sources = set()

    for result in results:
        asset = result.asset.copy()

        # Add sources to asset
        if result.sources:
            asset["Sources"] = "; ".join(result.sources[:3])  # Top 3 sources
            all_sources.update(result.sources)
        else:
            asset["Sources"] = ""

        assets.append(asset)

    return assets, list(all_sources)


# Sync wrapper for testing
def enrich_assets_sync(assets: list[dict], company: str) -> list[dict]:
    """Synchronous wrapper for testing."""
    async def run():
        results = await enrich_assets(assets, company, deep_fetch=False)
        enriched, _ = merge_enrichment_results(results)
        return enriched
    return asyncio.run(run())
