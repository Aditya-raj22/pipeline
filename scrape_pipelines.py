import os
import json
import asyncio
import hashlib
import time
from pathlib import Path
from typing import Optional
from dataclasses import dataclass

import httpx
from bs4 import BeautifulSoup
from openai import AsyncOpenAI
from pydantic import BaseModel, ValidationError
import pandas as pd
from dotenv import load_dotenv

load_dotenv()
client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# Config
CACHE_DIR = Path(".cache")
CACHE_TTL = 86400  # 24 hours
MAX_CONCURRENT = 5
TEXT_THRESHOLD = 500  # chars below this triggers JS fallback
VISION_THRESHOLD = 300  # chars below this after JS triggers vision
MAX_RETRIES = 3
RETRY_BACKOFF = [1, 3, 10]  # seconds

# Pydantic schema for validation + retry feedback
class PipelineAsset(BaseModel):
    therapeutic_area: str
    modality: str
    phase: str
    asset_name: str
    description: str
    therapeutic_target: str
    indication: str

class PipelineResponse(BaseModel):
    assets: list[PipelineAsset]

SCHEMA = PipelineResponse.model_json_schema()

# Cache helpers
def cache_key(url: str) -> str:
    return hashlib.md5(url.encode()).hexdigest()

def get_cached(url: str) -> Optional[str]:
    CACHE_DIR.mkdir(exist_ok=True)
    path = CACHE_DIR / f"{cache_key(url)}.json"
    if path.exists():
        data = json.loads(path.read_text())
        if time.time() - data["ts"] < CACHE_TTL:
            return data["content"]
    return None

def set_cache(url: str, content: str, content_type: str = "text"):
    CACHE_DIR.mkdir(exist_ok=True)
    path = CACHE_DIR / f"{cache_key(url)}.json"
    path.write_text(json.dumps({"ts": time.time(), "content": content, "type": content_type}))

# Lazy async playwright
_playwright = None
_browser = None

async def get_browser():
    global _playwright, _browser
    if _browser is None:
        from playwright.async_api import async_playwright
        _playwright = await async_playwright().start()
        _browser = await _playwright.chromium.launch(headless=True)
    return _browser

async def close_browser():
    global _playwright, _browser
    if _browser:
        await _browser.close()
    if _playwright:
        await _playwright.stop()
    _browser = None
    _playwright = None

def clean_html(html: str) -> str:
    """Extract meaningful text from HTML."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header", "aside", "noscript"]):
        tag.decompose()

    # Prioritize main content areas
    main = soup.find("main") or soup.find("article") or soup.find(class_=lambda x: x and "content" in x.lower() if x else False)
    if main:
        soup = main

    # Extract tables specially (common for pipeline data)
    tables = soup.find_all("table")
    table_text = ""
    for table in tables:
        rows = table.find_all("tr")
        for row in rows:
            cells = [cell.get_text(strip=True) for cell in row.find_all(["td", "th"])]
            table_text += " | ".join(cells) + "\n"

    text = soup.get_text(separator=" ", strip=True)
    if table_text:
        text = f"[TABLE DATA]\n{table_text}\n[END TABLE]\n\n{text}"

    return text[:50000]

async def fetch_with_httpx(url: str) -> tuple[str, int]:
    """Fast async HTTP fetch."""
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
        resp = await client.get(url, headers=headers)
        return resp.text, resp.status_code

async def fetch_with_playwright(url: str) -> tuple[str, Optional[bytes]]:
    """JS rendering + optional screenshot."""
    browser = await get_browser()
    page = await browser.new_page()
    try:
        await page.goto(url, wait_until="networkidle", timeout=60000)
        html = await page.content()
        screenshot = await page.screenshot(full_page=True, type="png")
        return html, screenshot
    finally:
        await page.close()

async def fetch_content(url: str) -> dict:
    """
    Three-tier fetch: HTTP -> JS rendering -> Vision
    Returns: {"text": str, "screenshot": bytes|None, "method": str}
    """
    # Check cache first
    cached = get_cached(url)
    if cached:
        print(f"    [cache hit]")
        return {"text": cached, "screenshot": None, "method": "cache"}

    # Tier 1: Fast HTTP
    try:
        html, status = await fetch_with_httpx(url)
        text = clean_html(html)

        if len(text) >= TEXT_THRESHOLD:
            set_cache(url, text)
            return {"text": text, "screenshot": None, "method": "http"}
        print(f"    [http: {len(text)} chars, trying JS...]")
    except Exception as e:
        print(f"    [http failed: {e}, trying JS...]")

    # Tier 2: JS rendering
    try:
        html, screenshot = await fetch_with_playwright(url)
        text = clean_html(html)

        if len(text) >= VISION_THRESHOLD:
            set_cache(url, text)
            return {"text": text, "screenshot": screenshot, "method": "playwright"}
        print(f"    [JS: {len(text)} chars, using vision...]")
        return {"text": text, "screenshot": screenshot, "method": "vision_needed"}
    except Exception as e:
        print(f"    [playwright failed: {e}]")
        return {"text": "", "screenshot": None, "method": "failed"}

async def extract_with_text(text: str, company: str, retry_errors: list[str] = None) -> PipelineResponse:
    """LLM extraction with Pydantic validation + retry feedback."""
    system = f"Extract all pharma pipeline assets from this {company} webpage. Return structured JSON."
    if retry_errors:
        system += f"\n\nPrevious attempt had validation errors - please fix:\n" + "\n".join(retry_errors)

    response = await client.responses.create(
        model="gpt-4.1-nano",
        input=[
            {"role": "system", "content": system},
            {"role": "user", "content": text}
        ],
        text={"format": {"type": "json_schema", "name": "pipeline_assets", "schema": SCHEMA}}
    )
    return PipelineResponse.model_validate_json(response.output_text)

async def extract_with_vision(screenshot: bytes, company: str) -> PipelineResponse:
    """Vision model extraction for image-heavy pages."""
    import base64
    b64 = base64.b64encode(screenshot).decode()

    response = await client.responses.create(
        model="gpt-4.1",  # Vision-capable model
        input=[
            {
                "role": "system",
                "content": f"Extract all pharma pipeline assets from this {company} pipeline page screenshot. Return structured JSON with all drug/therapy assets visible."
            },
            {
                "role": "user",
                "content": [
                    {"type": "input_image", "image_url": f"data:image/png;base64,{b64}"},
                    {"type": "input_text", "text": "Extract all pipeline assets from this image."}
                ]
            }
        ],
        text={"format": {"type": "json_schema", "name": "pipeline_assets", "schema": SCHEMA}}
    )
    return PipelineResponse.model_validate_json(response.output_text)

async def extract_pipeline(content: dict, company: str) -> PipelineResponse:
    """Extract with validation retry loop."""
    text = content["text"]
    screenshot = content.get("screenshot")
    method = content["method"]

    # Use vision if needed and available
    if method == "vision_needed" and screenshot:
        print(f"    [using vision model]")
        for attempt in range(MAX_RETRIES):
            try:
                return await extract_with_vision(screenshot, company)
            except ValidationError as e:
                if attempt < MAX_RETRIES - 1:
                    print(f"    [vision validation error, retry {attempt + 1}]")
                    await asyncio.sleep(RETRY_BACKOFF[attempt])
                else:
                    raise

    # Text-based extraction with retry
    errors = []
    for attempt in range(MAX_RETRIES):
        try:
            return await extract_with_text(text, company, errors if errors else None)
        except ValidationError as e:
            errors = [str(err) for err in e.errors()]
            if attempt < MAX_RETRIES - 1:
                print(f"    [validation error, retry {attempt + 1}]")
                await asyncio.sleep(RETRY_BACKOFF[attempt])
            else:
                raise

async def process_url(company: str, url: str, semaphore: asyncio.Semaphore) -> list[dict]:
    """Process single URL with concurrency control."""
    async with semaphore:
        print(f"Processing: {company}")
        try:
            content = await fetch_content(url)
            if content["method"] == "failed":
                print(f"  Error: Could not fetch content")
                return []

            result = await extract_pipeline(content, company)
            assets = [asset.model_dump() | {"company": company} for asset in result.assets]
            print(f"  Found {len(assets)} assets [{content['method']}]")
            return assets
        except Exception as e:
            print(f"  Error: {e}")
            return []

def load_urls(filepath="urls.txt"):
    """Load URLs from indexed file."""
    urls = []
    with open(filepath) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, val = line.split("=", 1)
                val = val.strip()
                if val:
                    urls.append((key.strip(), val))
    return urls

async def main():
    urls = load_urls()
    if not urls:
        print("No URLs found in urls.txt - add your URLs first")
        return

    print(f"Processing {len(urls)} URLs (max {MAX_CONCURRENT} concurrent)\n")

    semaphore = asyncio.Semaphore(MAX_CONCURRENT)
    tasks = [process_url(company, url, semaphore) for company, url in urls]
    results = await asyncio.gather(*tasks)

    all_assets = [asset for assets in results for asset in assets]

    await close_browser()

    if all_assets:
        df = pd.DataFrame(all_assets)
        cols = ["therapeutic_area", "modality", "phase", "asset_name", "description", "therapeutic_target", "indication", "company"]
        df = df[cols]
        df.columns = ["Therapeutic Area", "Modality", "Phase", "Asset Name", "Description", "Therapeutic Target", "Indication", "Company"]
        df.to_excel("pipeline_output.xlsx", index=False)
        print(f"\nDone! Saved {len(all_assets)} assets to pipeline_output.xlsx")
    else:
        print("\nNo assets extracted")

if __name__ == "__main__":
    asyncio.run(main())
