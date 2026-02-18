"""Content fetching - Playwright with tiled screenshots for vision extraction."""
import asyncio
from dataclasses import dataclass, field
from typing import Optional
from bs4 import BeautifulSoup
from config import config
from utils.cache import get_cached, set_cache

# Lazy playwright globals
_playwright = None
_browser = None

TILE_HEIGHT = 900   # px per screenshot tile
TILE_OVERLAP = 100  # px overlap to avoid cutting rows
MAX_TILES = 5       # cap tiles to keep cost reasonable
VIEWPORT_WIDTH = 1280


@dataclass
class FetchResult:
    """Result from content fetch."""
    text: str
    html: str
    screenshots: list[bytes] = field(default_factory=list)
    method: str = ""  # "playwright", "failed"
    links: list[str] = field(default_factory=list)

    # Back-compat: single screenshot property
    @property
    def screenshot(self) -> Optional[bytes]:
        return self.screenshots[0] if self.screenshots else None


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
    """Extract text and links from HTML."""
    import re
    soup = BeautifulSoup(html, "html.parser")

    # Extract links
    links = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href and not href.startswith(("#", "javascript:", "mailto:")):
            links.append(href)

    for tag in soup.find_all(onclick=True):
        onclick = tag.get("onclick", "")
        urls = re.findall(r"['\"]([^'\"]*\.php[^'\"]*)['\"]", onclick)
        links.extend(urls)

    # Remove non-content elements
    for tag in soup(["script", "style", "nav", "footer", "header", "aside", "noscript"]):
        tag.decompose()

    # Prioritize main content
    main = (
        soup.find("main") or
        soup.find("article") or
        soup.find(class_=lambda x: x and "content" in x.lower() if x else False)
    )
    if main:
        soup = main

    # Extract tables specially
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


async def fetch_content(url: str, use_cache: bool = True) -> FetchResult:
    """
    Fetch page with Playwright and take tiled screenshots.

    For long pages, takes multiple viewport-height screenshots with overlap
    so no content is lost. Text is also extracted for links/context.
    """
    # Check cache - but we still need screenshots for extraction
    # Cache is only useful for link discovery / text fallback
    cached_text = None
    if use_cache:
        cached = get_cached(url)
        if cached:
            cached_text = cached["content"]

    try:
        browser = await get_browser()
        page = await browser.new_page(viewport={"width": VIEWPORT_WIDTH, "height": TILE_HEIGHT})

        try:
            await page.goto(url, wait_until="networkidle", timeout=30000)
        except Exception:
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                await asyncio.sleep(3)
            except Exception:
                await page.close()
                return FetchResult(text=cached_text or "", html="", method="failed")

        # Wait for Cloudflare / JS
        for _ in range(5):
            content = await page.content()
            if "Just a moment" not in content and len(content) > 1000:
                break
            await asyncio.sleep(2)

        html = await page.content()
        text, links = clean_html(html)

        # Use cached text if richer
        if cached_text and len(cached_text) > len(text):
            text = cached_text
        else:
            set_cache(url, text)

        # Get page height for tiling
        page_height = await page.evaluate("document.body.scrollHeight")
        stride = TILE_HEIGHT - TILE_OVERLAP

        # Calculate tile positions
        tiles = []
        y = 0
        while y < page_height and len(tiles) < MAX_TILES:
            tiles.append(y)
            y += stride

        # Take tiled screenshots
        screenshots = []
        for y_offset in tiles:
            await page.evaluate(f"window.scrollTo(0, {y_offset})")
            await asyncio.sleep(0.3)  # let render settle
            shot = await page.screenshot(type="png")
            screenshots.append(shot)

        await page.close()

        return FetchResult(
            text=text,
            html=html,
            screenshots=screenshots,
            method="playwright",
            links=links,
        )

    except Exception as e:
        return FetchResult(
            text=cached_text or "",
            html="",
            method="failed",
        )


def resolve_url(base_url: str, href: str) -> str:
    """Resolve relative URL to absolute."""
    from urllib.parse import urljoin
    return urljoin(base_url, href)


def filter_pipeline_links(base_url: str, links: list[str], company: str) -> list[str]:
    """Filter links to find likely drug/pipeline detail pages."""
    import re
    from urllib.parse import urlparse

    base_domain = urlparse(base_url).netloc
    pipeline_links = []

    drug_code_pattern = re.compile(r'^/?[A-Z]{2,4}[-_]?\d{2,4}[A-Za-z]?$', re.IGNORECASE)
    drug_name_pattern = re.compile(r'^/?[A-Z][a-z]{4,}$')

    skip_patterns = [
        "news", "press", "career", "contact", "investor",
        "about", "team", "leadership", "login", "logout",
        "board", "history", "technology", "partner", "media",
        "procedure", "recruit", "executive", "bod", "sab"
    ]

    for href in links:
        url = resolve_url(base_url, href)
        parsed = urlparse(url)

        if parsed.netloc != base_domain:
            continue

        path = parsed.path
        path_lower = path.lower()

        if any(skip in path_lower for skip in skip_patterns):
            continue

        is_pipeline_page = any(pattern in path_lower for pattern in [
            "pipeline", "product", "drug", "candidate", "program",
            "rnd", "r-d", "research", "development"
        ])

        path_segment = path.rstrip('/').split('/')[-1] if path else ""
        is_drug_code = bool(drug_code_pattern.match(path_segment))
        is_drug_name = bool(drug_name_pattern.match(path_segment)) and len(path_segment) > 5

        if (is_pipeline_page or is_drug_code or is_drug_name):
            if url not in pipeline_links:
                pipeline_links.append(url)

    return pipeline_links
