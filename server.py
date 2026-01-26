"""FastAPI server for pipeline sourcing API."""
import asyncio
import json
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import AsyncGenerator

from config import config
from models.schema import UserSchema
from services.discovery import discover_pipeline_urls
from services.extraction import extract_pipeline
from services.schema_mapper import map_and_normalize
from services.enrichment import enrich_assets, merge_enrichment_results
from utils.fetch import close_browser

app = FastAPI(
    title="Pipeline Sourcer API",
    description="Pharma pipeline extraction system",
    version="2.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Configure for production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class PipelineEntry(BaseModel):
    company: str = ""
    url: str = ""

class PipelineRequest(BaseModel):
    entries: list[PipelineEntry] = []
    companies: list[str] = []  # Legacy support
    enrich: bool = True
    deep_fetch: bool = True


class Asset(BaseModel):
    therapeutic_area: str = ""
    modality: str = ""
    phase: str = ""
    asset_name: str = ""
    description: str = ""
    therapeutic_target: str = ""
    indication: str = ""
    company: str = ""
    latest_news: str = ""
    sources: str = ""


class PipelineResponse(BaseModel):
    assets: list[dict]
    companies_processed: int
    message: str = ""


async def process_company(
    company: str,
    schema: UserSchema,
    enrich: bool = True,
    deep_fetch: bool = True,
) -> list[dict]:
    """Process single company through pipeline."""
    # Discover URLs
    urls = await discover_pipeline_urls(company)
    if not urls:
        return []

    overview = next((u for u in urls if u.url_type == "overview"), urls[0])
    drug_urls = [u.url for u in urls if u.url_type == "drug_specific"]

    # Extract assets
    assets = await extract_pipeline(
        overview.url,
        company,
        drug_urls=drug_urls[:config.max_drug_pages_per_company],
    )
    if not assets:
        return []

    # Map to schema
    mapped = map_and_normalize(assets, schema)

    # Enrich
    if enrich:
        results = await enrich_assets(mapped, company, deep_fetch=deep_fetch)
        enriched, _ = merge_enrichment_results(results)
        return enriched

    return mapped


def convert_to_frontend_format(assets: list[dict]) -> list[dict]:
    """Convert internal format to frontend expected format."""
    # Schema mapper already outputs correct keys, just ensure all fields exist
    result = []
    for a in assets:
        result.append({
            "Asset Name": a.get("Asset Name", a.get("asset_name", "")),
            "Phase": a.get("Phase", a.get("phase", "")),
            "Therapeutic Area": a.get("Therapeutic Area", a.get("therapeutic_area", "")),
            "Modality": a.get("Modality", a.get("modality", "")),
            "Indication": a.get("Indication", a.get("indication", "")),
            "Therapeutic Target": a.get("Therapeutic Target", a.get("therapeutic_target", "")),
            "Description": a.get("Description", a.get("description", "")),
            "Company": a.get("Company", a.get("company", "")),
            "Latest News": a.get("Latest News", a.get("latest_news", "")),
            "Sources": a.get("Sources", a.get("sources", "")),
        })
    return result


@app.post("/api/pipeline", response_model=PipelineResponse)
async def run_pipeline(request: PipelineRequest):
    """Run pipeline for given companies."""
    if not request.companies:
        raise HTTPException(status_code=400, detail="No companies provided")

    schema = UserSchema.default()
    all_assets = []

    for company in request.companies:
        try:
            assets = await process_company(
                company,
                schema,
                enrich=request.enrich,
                deep_fetch=request.deep_fetch,
            )
            all_assets.extend(assets)
        except Exception as e:
            print(f"Error processing {company}: {e}")

    # Clean up browser
    await close_browser()

    return PipelineResponse(
        assets=convert_to_frontend_format(all_assets),
        companies_processed=len(request.companies),
        message=f"Processed {len(request.companies)} companies, found {len(all_assets)} assets",
    )


async def process_company_streaming(
    company: str,
    schema: UserSchema,
    enrich: bool,
    deep_fetch: bool,
) -> AsyncGenerator[tuple[str, list[dict]], None]:
    """Process company with streaming progress updates."""
    # Discover URLs
    yield f"[{company}] Discovering pipeline URLs...", []
    urls = await discover_pipeline_urls(company)

    if not urls:
        yield f"[{company}] No pipeline URLs found", []
        return

    overview = next((u for u in urls if u.url_type == "overview"), urls[0])
    drug_urls = [u.url for u in urls if u.url_type == "drug_specific"]
    yield f"[{company}] Found overview + {len(drug_urls)} drug pages", []

    # Extract assets
    yield f"[{company}] Extracting pipeline data...", []
    assets = await extract_pipeline(
        overview.url,
        company,
        drug_urls=drug_urls[:config.max_drug_pages_per_company],
    )

    if not assets:
        yield f"[{company}] No assets extracted", []
        return

    yield f"[{company}] Extracted {len(assets)} assets", []

    # Map to schema
    mapped = map_and_normalize(assets, schema)

    # Enrich
    if enrich:
        yield f"[{company}] Enriching with web search...", []
        results = await enrich_assets(mapped, company, deep_fetch=deep_fetch)
        enriched, _ = merge_enrichment_results(results)
        yield f"[{company}] Enrichment complete", enriched
    else:
        yield f"[{company}] Processing complete", mapped


def infer_company_from_url(url: str) -> str:
    """Infer company name from URL domain."""
    from urllib.parse import urlparse
    try:
        domain = urlparse(url).netloc
        # Remove www. and common TLDs
        name = domain.replace("www.", "").split(".")[0]
        # Capitalize and clean up
        return name.title()
    except:
        return "Unknown"


async def process_with_url(
    company: str,
    url: str,
    schema: UserSchema,
    enrich: bool,
    deep_fetch: bool,
) -> AsyncGenerator[tuple[str, list[dict]], None]:
    """Process with direct URL (skip discovery)."""
    # Infer company name from URL if not provided
    if not company:
        company = infer_company_from_url(url)

    yield f"[{company}] Using provided URL: {url}", []

    # Extract assets directly
    yield f"[{company}] Extracting pipeline data...", []
    assets = await extract_pipeline(url, company)

    if not assets:
        yield f"[{company}] No assets extracted", []
        return

    yield f"[{company}] Extracted {len(assets)} assets", []

    # Map to schema
    mapped = map_and_normalize(assets, schema)

    # Enrich
    if enrich:
        yield f"[{company}] Enriching with web search...", []
        results = await enrich_assets(mapped, company, deep_fetch=deep_fetch)
        enriched, _ = merge_enrichment_results(results)
        yield f"[{company}] Enrichment complete", enriched
    else:
        yield f"[{company}] Processing complete", mapped


async def stream_pipeline(request: PipelineRequest) -> AsyncGenerator[str, None]:
    """Stream pipeline progress as SSE events."""
    schema = UserSchema.default()
    all_assets = []

    # Handle both new entries format and legacy companies format
    entries = request.entries if request.entries else [PipelineEntry(company=c) for c in request.companies]
    total = len(entries)

    for i, entry in enumerate(entries):
        company = entry.company
        url = entry.url
        label = company or url or f"Entry {i+1}"
        progress = int((i / total) * 80)
        yield f"data: {json.dumps({'type': 'log', 'message': f'Processing {label} ({i+1}/{total})...', 'progress': progress})}\n\n"

        try:
            if url:
                # Direct URL provided - skip discovery
                async for message, assets in process_with_url(
                    company, url, schema, request.enrich, request.deep_fetch
                ):
                    yield f"data: {json.dumps({'type': 'log', 'message': message})}\n\n"
                    if assets:
                        all_assets.extend(assets)
            elif company:
                # No URL - use discovery
                async for message, assets in process_company_streaming(
                    company, schema, request.enrich, request.deep_fetch
                ):
                    yield f"data: {json.dumps({'type': 'log', 'message': message})}\n\n"
                    if assets:
                        all_assets.extend(assets)
        except Exception as e:
            yield f"data: {json.dumps({'type': 'log', 'message': f'[{label}] Error: {str(e)}'})}\n\n"

    # Clean up
    await close_browser()

    # Send final result
    yield f"data: {json.dumps({'type': 'complete', 'assets': convert_to_frontend_format(all_assets)})}\n\n"


@app.post("/api/pipeline/stream")
async def run_pipeline_stream(request: PipelineRequest):
    """Stream pipeline progress for given companies."""
    if not request.entries and not request.companies:
        raise HTTPException(status_code=400, detail="No entries provided")

    return StreamingResponse(
        stream_pipeline(request),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        }
    )


@app.get("/health")
async def health():
    """Health check endpoint."""
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
