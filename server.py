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
from services.drug_pages import enrich_from_drug_pages
from utils.fetch import close_browser

app = FastAPI(title="Pipeline Sourcer API", version="3.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class PipelineEntry(BaseModel):
    company: str = ""
    url: str = ""

class PipelineRequest(BaseModel):
    entries: list[PipelineEntry] = []
    companies: list[str] = []
    drug_pages: bool = False


RESULT_COLUMNS = [
    "Asset Name", "Phase", "Therapeutic Area", "Modality",
    "Indication", "Therapeutic Target", "Description", "Company", "Sources",
]


def convert_to_frontend_format(assets: list[dict]) -> list[dict]:
    return [
        {col: a.get(col, "") for col in RESULT_COLUMNS}
        for a in assets
    ]


def infer_company_from_url(url: str) -> str:
    from urllib.parse import urlparse
    try:
        domain = urlparse(url).netloc
        return domain.replace("www.", "").split(".")[0].title()
    except Exception:
        return "Unknown"


async def _process_entry(
    entry: PipelineEntry,
    schema: UserSchema,
    drug_pages: bool,
    queue: asyncio.Queue,
) -> list[dict]:
    """Process one company/URL entry."""
    company = entry.company
    url = entry.url.strip() if entry.url else None

    if url and not company:
        company = infer_company_from_url(url)

    label = company or url or "Unknown"

    try:
        drug_links = []
        if url:
            await queue.put(f"[{company}] Using provided URL: {url}")
            await queue.put(f"[{company}] Extracting pipeline data...")
            assets, drug_links = await extract_pipeline(url, company)
        else:
            await queue.put(f"[{company}] Discovering pipeline URLs...")
            urls = await discover_pipeline_urls(company)
            if not urls:
                await queue.put(f"[{company}] No pipeline URLs found")
                return []
            overview = next((u for u in urls if u.url_type == "overview"), urls[0])
            await queue.put(f"[{company}] Found: {overview.url}")
            await queue.put(f"[{company}] Extracting pipeline data...")
            assets, drug_links = await extract_pipeline(overview.url, company)

        if not assets:
            await queue.put(f"[{company}] No assets extracted")
            return []

        await queue.put(f"[{company}] Extracted {len(assets)} assets")
        mapped = map_and_normalize(assets, schema)

        if drug_pages:
            async def on_progress(msg):
                await queue.put(msg)
            mapped = await enrich_from_drug_pages(mapped, company, on_progress=on_progress, overview_links=drug_links)

        await queue.put(f"[{company}] Complete")
        return mapped

    except Exception as e:
        await queue.put(f"[{label}] Error: {str(e)}")
        return []


async def stream_pipeline(request: PipelineRequest) -> AsyncGenerator[str, None]:
    schema = UserSchema.default()
    entries = request.entries if request.entries else [PipelineEntry(company=c) for c in request.companies]
    total = len(entries)
    queue: asyncio.Queue = asyncio.Queue()

    async def run_serial():
        all_assets = []
        for i, entry in enumerate(entries):
            progress = int((i / total) * 80)
            label = entry.company or entry.url or f"Entry {i+1}"
            await queue.put(json.dumps({"type": "log", "message": f"Processing {label} ({i+1}/{total})...", "progress": progress}))
            try:
                assets = await _process_entry(entry, schema, request.drug_pages, queue)
                all_assets.extend(assets)
            except Exception as e:
                await queue.put(json.dumps({"type": "log", "message": f"[{label}] Error: {e}"}))
        await close_browser()
        await queue.put(json.dumps({
            "type": "complete",
            "assets": convert_to_frontend_format(all_assets),
        }))
        await queue.put(None)

    asyncio.create_task(run_serial())

    while True:
        item = await queue.get()
        if item is None:
            break
        if item.startswith("{"):
            yield f"data: {item}\n\n"
        else:
            yield f"data: {json.dumps({'type': 'log', 'message': item})}\n\n"


@app.post("/api/pipeline/stream")
async def run_pipeline_stream(request: PipelineRequest):
    if not request.entries and not request.companies:
        raise HTTPException(status_code=400, detail="No entries provided")

    return StreamingResponse(
        stream_pipeline(request),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )


@app.get("/health")
async def health():
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
