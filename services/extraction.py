"""Pipeline extraction service - vision-first with tiled screenshots."""
import asyncio
import base64
from openai import AsyncOpenAI
from pydantic import ValidationError
from config import config
from models.extracted import ExtractedAsset, PipelineResponse
from utils.fetch import FetchResult, fetch_content, filter_pipeline_links, close_browser

client = AsyncOpenAI(api_key=config.openai_api_key)


def _make_schema():
    """Generate OpenAI-compatible JSON schema."""
    schema = PipelineResponse.model_json_schema()

    def fix_for_openai(obj):
        if isinstance(obj, dict):
            if "properties" in obj:
                obj["additionalProperties"] = False
                obj["required"] = list(obj["properties"].keys())
            for v in obj.values():
                fix_for_openai(v)
        elif isinstance(obj, list):
            for item in obj:
                fix_for_openai(item)

    fix_for_openai(schema)
    if "$defs" in schema:
        for defn in schema["$defs"].values():
            fix_for_openai(defn)
    return schema


SCHEMA = _make_schema()


TEXT_PROMPT = """Extract ALL pharmaceutical pipeline assets from this text content of a pipeline page.
Company: {company}
Source: {url}

Page content:
{text}

IMPORTANT: An "asset" is a DRUG or COMPOUND being developed, identified by:
- A code name (e.g., "ABL001", "ORM-1153", "SKI-O-703")
- A proprietary drug name (e.g., "Lazertinib", "Cevidoplenib")
- "TBD" or "Undisclosed" if the drug name is not yet announced - INCLUDE THESE

Do NOT create assets from:
- Disease names alone (these are INDICATIONS)
- Target names alone (these are THERAPEUTIC TARGETS)
- Modality types alone (these are MODALITIES)

MULTIPLE INDICATIONS:
- Same phase: combine with semicolons (e.g., "NSCLC; Breast Cancer")
- Different phases: create separate entries

For each DRUG asset extract:
- asset_name: Drug code or name ("TBD" if undisclosed)
- therapeutic_area: e.g., "Oncology", "Neurology"
- modality: e.g., "Small molecule", "Bispecific Antibody"
- phase: Preclinical, IND-enabling, Phase 1, Phase 1/2, Phase 2, Phase 3, Filed, Approved
- description: Mechanism of action
- therapeutic_target: Molecular target (e.g., "CD33", "EGFR"). If target is undisclosed/masked (e.g., "Target A"), use empty string.
- indication: The DISEASE being treated (e.g., "AML", "NSCLC"). Must be a disease/condition, NOT a target, NOT a modality, NOT a treatment regimen. If undisclosed, use empty string.

CRITICAL: Do NOT miss any assets. Extract EVERY drug mentioned.
Use empty string for unknown fields."""


VISION_PROMPT = """Extract ALL pharmaceutical pipeline assets from these screenshots of a pipeline page.
Company: {company}
Source: {url}

The screenshots are TILED sections of the same page, shown top-to-bottom. Together they show the complete page.

IMPORTANT: An "asset" is a DRUG or COMPOUND being developed, identified by:
- A code name (e.g., "ABL001", "ORM-1153", "SKI-O-703")
- A proprietary drug name (e.g., "Lazertinib", "Cevidoplenib")
- "TBD" or "Undisclosed" if the drug name is not yet announced - INCLUDE THESE

Do NOT create assets from:
- Disease names alone (these are INDICATIONS)
- Target names alone (these are THERAPEUTIC TARGETS)
- Modality types alone (these are MODALITIES)

MULTIPLE INDICATIONS:
- Same phase: combine with semicolons (e.g., "NSCLC; Breast Cancer")
- Different phases: create separate entries

Look for assets in:
- Pipeline tables/charts with phase columns
- Honeycomb/hexagon diagrams
- Bar charts showing development stages
- Text descriptions of programs
- Partnership/deal sections mentioning specific drugs

For each DRUG asset extract:
- asset_name: Drug code or name ("TBD" if undisclosed)
- therapeutic_area: e.g., "Oncology", "Neurology"
- modality: e.g., "Small molecule", "Bispecific Antibody"
- phase: Preclinical, IND-enabling, Phase 1, Phase 1/2, Phase 2, Phase 3, Filed, Approved
- description: Mechanism of action
- therapeutic_target: Molecular target (e.g., "CD33", "EGFR"). If target is undisclosed/masked (e.g., "Target A"), use empty string.
- indication: The DISEASE being treated (e.g., "AML", "NSCLC"). Must be a disease/condition, NOT a target, NOT a modality, NOT a treatment regimen. If undisclosed, use empty string.

DEDUPLICATION: Each unique drug should appear ONLY ONCE. If the same drug appears in multiple tiles (overlapping screenshots), extract it only from the first occurrence.

CRITICAL: Do NOT miss any assets. Extract EVERY drug visible across all screenshots.
Use empty string for unknown fields."""


async def extract_with_vision(
    screenshots: list[bytes],
    company: str,
    url: str,
    text: str = "",
) -> PipelineResponse:
    """Vision extraction from tiled screenshots."""
    content_parts = []

    # Add each screenshot tile
    for i, shot in enumerate(screenshots):
        b64 = base64.b64encode(shot).decode()
        content_parts.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/png;base64,{b64}"}
        })

    # Add any extracted text as supplementary context
    if text and len(text) > 100:
        content_parts.append({
            "type": "text",
            "text": f"Supplementary text from same page:\n{text[:10000]}"
        })

    content_parts.append({
        "type": "text",
        "text": f"Extract all pipeline assets from these {len(screenshots)} screenshot tiles of the page."
    })

    response = await client.chat.completions.create(
        model=config.vision_model,
        messages=[
            {"role": "system", "content": VISION_PROMPT.format(company=company, url=url)},
            {"role": "user", "content": content_parts}
        ],
        response_format={
            "type": "json_schema",
            "json_schema": {"name": "pipeline_assets", "strict": True, "schema": SCHEMA}
        }
    )
    return PipelineResponse.model_validate_json(response.choices[0].message.content)


async def extract_with_text(text: str, company: str, url: str) -> list[ExtractedAsset]:
    """Text-only extraction — fast (~3s), cheap, works great for HTML tables."""
    try:
        response = await client.chat.completions.create(
            model=config.text_model,
            messages=[{"role": "user", "content": TEXT_PROMPT.format(
                company=company, url=url, text=text[:15000],
            )}],
            response_format={
                "type": "json_schema",
                "json_schema": {"name": "pipeline_assets", "strict": True, "schema": SCHEMA}
            },
        )
        result = PipelineResponse.model_validate_json(response.choices[0].message.content)
        return [
            ExtractedAsset.from_llm(a, company=company, source_url=url, extraction_method="text")
            for a in result.assets
        ]
    except Exception as e:
        print(f"    Text extraction failed: {e}")
        return []


async def extract_from_content(
    content: FetchResult,
    company: str,
    url: str,
) -> list[ExtractedAsset]:
    """Extract assets — text-first, vision fallback."""
    if content.method == "failed":
        return []

    # Fast path: text-only extraction if page has rich text (tables, lists)
    if content.text and len(content.text) >= config.hybrid_threshold:
        text_assets = await extract_with_text(content.text, company, url)
        if text_assets:
            print(f"    Text extraction: {len(text_assets)} assets (fast path)")
            return text_assets
        print(f"    Text extraction yielded 0 — falling back to vision")

    # Vision fallback for graphical pages
    if not content.screenshots:
        return []

    errors = []
    for attempt in range(config.max_retries):
        try:
            result = await extract_with_vision(
                content.screenshots, company, url, content.text
            )

            assets = [
                ExtractedAsset.from_llm(
                    llm_asset,
                    company=company,
                    source_url=url,
                    extraction_method="vision",
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


def normalize_asset_name(name: str) -> str:
    """Normalize asset name for matching."""
    import re
    name = re.sub(r'\s*\([^)]+\)', '', name)
    name = name.split()[0] if name else name
    return name.strip().upper()


def merge_assets(existing: list[ExtractedAsset], new: list[ExtractedAsset]) -> list[ExtractedAsset]:
    """Merge new assets into existing list (only updates, no new additions)."""
    by_name = {}
    for a in existing:
        norm_name = normalize_asset_name(a.asset_name)
        if norm_name not in by_name:
            by_name[norm_name] = a

    for asset in new:
        norm_name = normalize_asset_name(asset.asset_name)
        if norm_name in by_name:
            old = by_name[norm_name]
            for field in ["therapeutic_area", "modality", "description",
                          "therapeutic_target", "indication"]:
                new_val = getattr(asset, field)
                old_val = getattr(old, field)
                if new_val and new_val not in ["", "TBD", "Undisclosed"]:
                    if not old_val or old_val in ["", "TBD", "Undisclosed"]:
                        setattr(old, field, new_val)
                    elif new_val not in old_val:
                        setattr(old, field, f"{old_val}; {new_val}")

    return list(by_name.values())


def deduplicate_assets(assets: list[ExtractedAsset]) -> list[ExtractedAsset]:
    """Remove duplicate assets (same drug extracted from overlapping tiles)."""
    seen = {}
    for a in assets:
        key = normalize_asset_name(a.asset_name)
        if not key or key in ("TBD", "UNDISCLOSED"):
            seen[id(a)] = a  # keep all unnamed assets
            continue
        if key not in seen:
            seen[key] = a
        else:
            # Merge richer data into existing
            existing = seen[key]
            for field in ["therapeutic_area", "modality", "description",
                          "therapeutic_target", "indication"]:
                new_val = getattr(a, field)
                old_val = getattr(existing, field)
                if new_val and new_val not in ("", "TBD", "Undisclosed") and (
                    not old_val or old_val in ("", "TBD", "Undisclosed")
                ):
                    setattr(existing, field, new_val)
    return list(seen.values())


async def extract_pipeline(
    overview_url: str,
    company: str,
) -> tuple[list[ExtractedAsset], list[str]]:
    """Extract pipeline assets + drug page links from overview page.

    Returns (assets, drug_page_links) tuple.
    """
    print(f"  Fetching overview: {overview_url}")
    overview_content = await fetch_content(overview_url)
    assets = await extract_from_content(overview_content, company, overview_url)
    before = len(assets)
    assets = deduplicate_assets(assets)
    deduped = before - len(assets)

    # Extract drug page links from overview HTML for enrichment
    drug_links = filter_pipeline_links(overview_url, overview_content.links, company) if overview_content.links else []

    method = assets[0].extraction_method if assets else "none"
    msg = f"  Found {len(assets)} assets via {method}"
    if deduped:
        msg += f" ({deduped} dupes removed)"
    if drug_links:
        msg += f", {len(drug_links)} drug page links"
    print(msg)
    return assets, drug_links


# Sync wrapper for testing
def extract_pipeline_sync(overview_url: str, company: str) -> tuple[list[ExtractedAsset], list[str]]:
    """Synchronous wrapper for extract_pipeline."""
    async def run():
        result = await extract_pipeline(overview_url, company)
        await close_browser()
        return result
    return asyncio.run(run())
