"""Content fetching utilities with 3-tier fallback."""
import asyncio
from dataclasses import dataclass
from typing import Optional
import httpx
from bs4 import BeautifulSoup
from config import config
from utils.cache import get_cached, set_cache

# Lazy playwright globals
_playwright = None
_browser = None


@dataclass
class FetchResult:
    """Result from content fetch."""
    text: str
    html: str
    screenshot: Optional[bytes]
    method: str  # "cache", "http", "playwright", "vision_needed", "failed"
    links: list[str] = None  # Links found on page

    def __post_init__(self):
        if self.links is None:
            self.links = []


async def get_browser():
    """Lazy init playwright browser."""
    global _playwright, _browser
    if _browser is None:
        from playwright.async_api import async_playwright
        _playwright = await async_playwright().start()
        _browser = await _playwright.chromium.launch(headless=True)
    return _browser


async def close_browser():
    """Clean up playwright resources."""
    global _playwright, _browser
    if _browser:
        await _browser.close()
    if _playwright:
        await _playwright.stop()
    _browser = None
    _playwright = None


def clean_html(html: str) -> tuple[str, list[str]]:
    """
    Extract meaningful text and links from HTML.

    Returns: (text, list of links)
    """
    import re
    soup = BeautifulSoup(html, "html.parser")

    # Extract links from anchor tags
    links = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href and not href.startswith(("#", "javascript:", "mailto:")):
            links.append(href)

    # Also extract links from onclick handlers and other attributes
    # This catches JS-based navigation
    for tag in soup.find_all(onclick=True):
        onclick = tag.get("onclick", "")
        # Find URLs in onclick handlers
        urls = re.findall(r"['\"]([^'\"]*\.php[^'\"]*)['\"]", onclick)
        links.extend(urls)

    # Find URL patterns in raw HTML (for JS-constructed links)
    url_patterns = re.findall(r'(?:href|src|url)[=:]\s*["\']?([^"\'>\s]+\.php)', html, re.IGNORECASE)
    links.extend(url_patterns)

    # Remove non-content elements
    for tag in soup(["script", "style", "nav", "footer", "header", "aside", "noscript"]):
        tag.decompose()

    # Prioritize main content areas
    main = (
        soup.find("main") or
        soup.find("article") or
        soup.find(class_=lambda x: x and "content" in x.lower() if x else False)
    )
    if main:
        soup = main

    # Extract tables specially (common for pipeline data)
    tables = soup.find_all("table")
    table_text = ""
    for table in tables:
        rows = table.find_all("tr")
        for row in rows:
            cells = [cell.get_text(strip=True) for cell in row.find_all(["td", "th"])]
            if cells:
                table_text += " | ".join(cells) + "\n"

    text = soup.get_text(separator=" ", strip=True)
    if table_text:
        text = f"[TABLE DATA]\n{table_text}[END TABLE]\n\n{text}"

    return text[:50000], links


async def fetch_with_httpx(url: str) -> tuple[str, int]:
    """Fast async HTTP fetch. Returns (html, status_code)."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }
    async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
        resp = await client.get(url, headers=headers)
        return resp.text, resp.status_code


async def fetch_with_playwright(url: str) -> tuple[str, Optional[bytes]]:
    """JS rendering + screenshot. Returns (html, screenshot_bytes)."""
    import asyncio as aio

    browser = await get_browser()
    page = await browser.new_page()
    try:
        # Try networkidle first, fall back to domcontentloaded
        try:
            await page.goto(url, wait_until="networkidle", timeout=30000)
        except Exception:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            # Wait a bit for JS to execute
            await aio.sleep(3)

        # Additional wait for Cloudflare challenges
        for _ in range(5):
            content = await page.content()
            if "Just a moment" not in content and len(content) > 1000:
                break
            await aio.sleep(2)

        html = await page.content()
        screenshot = await page.screenshot(full_page=True, type="png")
        return html, screenshot
    finally:
        await page.close()


async def fetch_content(url: str, use_cache: bool = True) -> FetchResult:
    """
    Three-tier fetch: Cache -> HTTP -> JS rendering -> Vision needed

    Returns FetchResult with text, screenshot (if needed), and method used.
    """
    # Check cache first
    if use_cache:
        cached = get_cached(url)
        if cached:
            return FetchResult(
                text=cached["content"],
                html="",
                screenshot=None,
                method="cache",
            )

    # Tier 1: Fast HTTP
    html = ""
    try:
        html, status = await fetch_with_httpx(url)
        text, links = clean_html(html)

        if len(text) >= config.text_threshold:
            set_cache(url, text)
            return FetchResult(
                text=text,
                html=html,
                screenshot=None,
                method="http",
                links=links,
            )
    except Exception as e:
        pass  # Fall through to playwright

    # Tier 2: JS rendering
    try:
        html, screenshot = await fetch_with_playwright(url)
        text, links = clean_html(html)

        if len(text) >= config.vision_threshold:
            set_cache(url, text)
            return FetchResult(
                text=text,
                html=html,
                screenshot=screenshot,
                method="playwright",
                links=links,
            )

        # Text too short, vision needed
        return FetchResult(
            text=text,
            html=html,
            screenshot=screenshot,
            method="vision_needed",
            links=links,
        )
    except Exception as e:
        return FetchResult(
            text="",
            html="",
            screenshot=None,
            method="failed",
        )


def resolve_url(base_url: str, href: str) -> str:
    """Resolve relative URL to absolute."""
    from urllib.parse import urljoin
    return urljoin(base_url, href)


def filter_pipeline_links(base_url: str, links: list[str], company: str) -> list[str]:
    """
    Filter links to find likely drug/pipeline detail pages.

    Returns list of absolute URLs.
    """
    from urllib.parse import urlparse

    base_domain = urlparse(base_url).netloc
    pipeline_links = []

    for href in links:
        url = resolve_url(base_url, href)
        parsed = urlparse(url)

        # Must be same domain
        if parsed.netloc != base_domain:
            continue

        path = parsed.path.lower()

        # Look for pipeline/drug page patterns
        if any(pattern in path for pattern in [
            "pipeline", "product", "drug", "candidate", "program",
            "rnd", "r-d", "research", "development"
        ]):
            # Exclude non-detail pages
            if not any(skip in path for skip in [
                "news", "press", "career", "contact", "investor",
                "about", "team", "leadership"
            ]):
                if url not in pipeline_links:
                    pipeline_links.append(url)

    return pipeline_links
