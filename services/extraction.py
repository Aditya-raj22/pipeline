"""Pipeline extraction service - extracts assets from pipeline pages."""
import asyncio
import base64
from typing import Optional
from openai import AsyncOpenAI
from pydantic import ValidationError
from config import config
from models.extracted import ExtractedAsset, PipelineResponse, LLMAsset
from utils.fetch import FetchResult, fetch_content, filter_pipeline_links, close_browser

client = AsyncOpenAI(api_key=config.openai_api_key)


def _make_schema():
    """Generate OpenAI-compatible JSON schema."""
    schema = PipelineResponse.model_json_schema()

    def fix_for_openai(obj):
        """Fix schema for OpenAI strict mode requirements."""
        if isinstance(obj, dict):
            # OpenAI requires additionalProperties: false
            if "properties" in obj:
                obj["additionalProperties"] = False
                # OpenAI requires ALL properties in required array
                obj["required"] = list(obj["properties"].keys())
            for v in obj.values():
                fix_for_openai(v)
        elif isinstance(obj, list):
            for item in obj:
                fix_for_openai(item)

    fix_for_openai(schema)
    # Also fix $defs
    if "$defs" in schema:
        for defn in schema["$defs"].values():
            fix_for_openai(defn)
    return schema


SCHEMA = _make_schema()


EXTRACTION_PROMPT = """Extract all pharmaceutical pipeline assets from this content.
Company: {company}
Source: {url}

Content:
{content}

For each asset, extract these fields:
- therapeutic_area: e.g., "Oncology", "Neurology", "Ophthalmology", "Dermatology / Fibrosis"
- modality: Include delivery route if stated, e.g., "Bispecific Antibody", "GalNAc-asiRNA (subcutaneous)"
- phase: Use exact value from page. Valid: Preclinical, Phase 1, Phase 1/2, Phase 2, Phase 2/3, Phase 3, Filed, Approved, IND enabling study, Phase 1 completed, Discovery, Platform
- asset_name: Drug/compound code (e.g., "ABL001", "OLX10212") or name
- description: Mechanism of action or brief summary
- therapeutic_target: Molecular target (e.g., "VEGF/DLL4", "PD-L1/4-1BB")
- indication: Disease/condition

Return JSON array of assets. Use empty string for unknown fields."""

VISION_PROMPT = """Extract pharmaceutical pipeline data from this image.
Company: {company}

The image may contain:
- Tables with drug information
- Visual phase indicators (colored bars showing development stage)
- Pipeline charts or diagrams

For visual phase indicators:
- Solid filled section = completed
- Partial fill or current marker = ongoing
- Map to: Preclinical, Phase 1, Phase 1/2, Phase 2, Phase 2/3, Phase 3, Filed, Approved

Extract ALL assets visible. Return JSON matching schema."""


async def extract_with_text(
    text: str,
    company: str,
    url: str,
    retry_errors: list[str] = None,
) -> PipelineResponse:
    """LLM extraction from text with validation retry."""
    prompt = EXTRACTION_PROMPT.format(company=company, url=url, content=text[:40000])

    if retry_errors:
        prompt += f"\n\nPrevious attempt had errors - please fix:\n" + "\n".join(retry_errors)

    response = await client.chat.completions.create(
        model=config.text_model,
        messages=[
            {"role": "system", "content": "You extract pharma pipeline data. Return valid JSON only."},
            {"role": "user", "content": prompt}
        ],
        response_format={
            "type": "json_schema",
            "json_schema": {"name": "pipeline_assets", "strict": True, "schema": SCHEMA}
        }
    )
    return PipelineResponse.model_validate_json(response.choices[0].message.content)


async def extract_with_vision(
    screenshot: bytes,
    company: str,
) -> PipelineResponse:
    """Vision model extraction for image-heavy pages."""
    b64 = base64.b64encode(screenshot).decode()

    response = await client.chat.completions.create(
        model=config.vision_model,
        messages=[
            {"role": "system", "content": VISION_PROMPT.format(company=company)},
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                    {"type": "text", "text": "Extract all pipeline assets from this image."}
                ]
            }
        ],
        response_format={
            "type": "json_schema",
            "json_schema": {"name": "pipeline_assets", "strict": True, "schema": SCHEMA}
        }
    )
    return PipelineResponse.model_validate_json(response.choices[0].message.content)


async def extract_from_content(
    content: FetchResult,
    company: str,
    url: str,
) -> list[ExtractedAsset]:
    """Extract assets from fetched content with retry loop."""
    if content.method == "failed":
        return []

    # Determine extraction method
    use_vision = content.method == "vision_needed" and content.screenshot
    method = "vision" if use_vision else "text"

    errors = []
    for attempt in range(config.max_retries):
        try:
            if use_vision:
                result = await extract_with_vision(content.screenshot, company)
            else:
                result = await extract_with_text(content.text, company, url, errors if errors else None)

            # Convert LLMAsset to ExtractedAsset with metadata
            assets = [
                ExtractedAsset.from_llm(
                    llm_asset,
                    company=company,
                    source_url=url,
                    extraction_method=method,
                )
                for llm_asset in result.assets
            ]
            return assets

        except ValidationError as e:
            errors = [str(err) for err in e.errors()]
            if attempt < config.max_retries - 1:
                await asyncio.sleep(config.retry_backoff[attempt])
            else:
                print(f"    Extraction failed after {config.max_retries} attempts")
                return []
        except Exception as e:
            print(f"    Extraction error: {e}")
            return []

    return []


def merge_assets(existing: list[ExtractedAsset], new: list[ExtractedAsset]) -> list[ExtractedAsset]:
    """
    Merge new assets into existing list.
    Updates existing assets with more detail, adds new ones.
    """
    # Index by asset_name
    by_name = {a.asset_name.upper(): a for a in existing}

    for asset in new:
        key = asset.asset_name.upper()
        if key in by_name:
            # Merge: prefer non-empty values from new
            old = by_name[key]
            for field in ["therapeutic_area", "modality", "phase", "description",
                          "therapeutic_target", "indication"]:
                new_val = getattr(asset, field)
                old_val = getattr(old, field)
                if new_val and (not old_val or old_val == ""):
                    setattr(old, field, new_val)
        else:
            by_name[key] = asset

    return list(by_name.values())


async def extract_pipeline(
    overview_url: str,
    company: str,
    drug_urls: list[str] = None,
    max_drug_pages: int = None,
) -> list[ExtractedAsset]:
    """
    Extract pipeline assets from overview + optional drug-specific pages.

    Args:
        overview_url: Main pipeline page URL
        company: Company name
        drug_urls: Optional list of drug-specific page URLs
        max_drug_pages: Max drug pages to fetch (default from config)

    Returns:
        List of ExtractedAsset objects
    """
    if max_drug_pages is None:
        max_drug_pages = config.max_drug_pages_per_company

    # Fetch and extract from overview
    print(f"  Fetching overview: {overview_url}")
    overview_content = await fetch_content(overview_url)
    assets = await extract_from_content(overview_content, company, overview_url)
    print(f"  Found {len(assets)} assets from overview [{overview_content.method}]")

    # Discover drug page links if not provided
    if drug_urls is None and overview_content.links:
        drug_urls = filter_pipeline_links(overview_url, overview_content.links, company)
        print(f"  Discovered {len(drug_urls)} drug page links")

    # Fetch drug-specific pages in parallel
    if drug_urls:
        drug_urls = drug_urls[:max_drug_pages]
        print(f"  Fetching {len(drug_urls)} drug pages...")

        tasks = [fetch_content(url) for url in drug_urls]
        drug_contents = await asyncio.gather(*tasks, return_exceptions=True)

        for url, content in zip(drug_urls, drug_contents):
            if isinstance(content, Exception):
                continue
            if content.method != "failed":
                drug_assets = await extract_from_content(content, company, url)
                if drug_assets:
                    assets = merge_assets(assets, drug_assets)

    return assets


async def extract_pipeline_for_company(
    company: str,
    urls: list[str],
) -> list[ExtractedAsset]:
    """
    High-level extraction for a company given discovered URLs.

    Picks best overview URL and extracts from it + drug pages.
    """
    if not urls:
        print(f"  No URLs for {company}")
        return []

    # First URL should be overview (already sorted by discovery)
    overview_url = urls[0]

    # Rest could be drug-specific
    drug_urls = urls[1:] if len(urls) > 1 else None

    return await extract_pipeline(overview_url, company, drug_urls)


# Sync wrapper for testing
def extract_pipeline_sync(overview_url: str, company: str) -> list[ExtractedAsset]:
    """Synchronous wrapper for extract_pipeline."""
    async def run():
        result = await extract_pipeline(overview_url, company)
        await close_browser()
        return result
    return asyncio.run(run())
