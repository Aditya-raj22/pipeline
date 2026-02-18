# Technical Specification: Pipeline Sourcing System v3

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        Entry Points                             │
│   CLI (main.py)  │  FastAPI (server.py)  │  Streamlit (app.py)  │
└────────┬─────────┴──────────┬────────────┴──────────┬───────────┘
         │                    │                       │
         └────────────────────┼───────────────────────┘
                              ▼
                    process_company(name, url?)
                              │
              ┌───────────────┼───────────────┐
              ▼               ▼               ▼
         Discovery       Extraction      Enrichment
         (Step 1)        (Steps 2-3)     (Step 4)
              │               │               │
              ▼               ▼               ▼
         services/       services/        services/
         discovery.py    extraction.py    drug_pages.py
              │               │               │
              ▼               ▼               ▼
         utils/search    utils/fetch      utils/fetch
         (DDG)           (Playwright)     (httpx + PW)
```

## Data Flow

### Step 1: Discovery (`services/discovery.py`)

**Input:** Company name (string)
**Output:** Ranked list of `DiscoveredURL` objects

Two strategies run in sequence:

1. **Direct URL probing (~1s)** — Generates candidate URLs from company name (e.g., `ablbio.com/pipeline`, `ablbio.com/en/pipeline`) and sends HEAD requests. If any return 200, discovery is complete.

2. **DuckDuckGo fallback (~3s)** — Fires 3 parallel searches:
   - `site:{domain} pipeline`
   - `{company} pipeline`
   - `{company} drug candidates clinical trials`

   Results are classified by heuristic rules (URL patterns, domain matching) into: `overview`, `drug_specific`, `news`, `irrelevant`. Sorted by type priority + relevance score.

**Skip condition:** If a URL is provided (via Excel upload or API), discovery is bypassed entirely.

### Step 2: Extraction (`services/extraction.py`)

**Input:** Pipeline page URL + company name
**Output:** List of `ExtractedAsset` objects + drug page links

Fetching (`utils/fetch.py`):
- **httpx** for initial HTML fetch — fast, lightweight
- **Playwright** (headless Chromium) for JS-rendered pages and screenshots
- Tiled screenshots (1280x800 viewport, scrolled in 700px increments) for full-page capture

Extraction strategy (text-first, vision fallback):

1. **Text extraction** — If page text > 3000 chars, send to LLM with structured JSON schema. Fast (~3s), cheap, works great for HTML tables.

2. **Vision fallback** — If text extraction yields 0 assets (graphical pipeline), send tiled screenshots to vision model. Handles charts, hexagons, bar diagrams.

3. **Deduplication** — Same drug from overlapping screenshot tiles is merged. Normalization by uppercase name, first token.

### Step 3: Schema Mapping (`services/schema_mapper.py`)

Maps `ExtractedAsset` fields to the user schema's 9-column output format. Fills missing fields with "Undisclosed".

### Step 4: Enrichment (`services/drug_pages.py`) — Optional

**Input:** Mapped assets with gaps
**Output:** Assets with filled indication, target, description, etc.

For each asset needing enrichment:

1. **Snippet-based fill (zero cost)** — DDG search for `"{drug}" "{company}"`, parse result snippets with LLM. If all gaps filled, skip page fetching.

2. **Page fetch fill** — Rank URLs (company site > clinicaltrials.gov > drug databases > other). Fetch top 3 pages via httpx (Playwright fallback for JS pages). Combine text, send to LLM to fill remaining gaps.

**Concurrency:** 5 concurrent DDG searches, 3 URLs per asset max. Overview page links (from Step 2) are prepended as highest-priority sources.

## Models

| Model | Usage | Why |
|-------|-------|-----|
| `gpt-4.1-mini` | Text extraction, vision extraction, enrichment parsing | Best accuracy/cost for structured extraction. 72.7% MMMU vision score, 1M context window. $0.40/1M input, $1.60/1M output. |

Single model simplifies configuration and caching. Vision and text use the same model since gpt-4.1-mini handles both modalities well.

## Caching

- **File-based cache** in `.cache/` directory
- **Key:** MD5 hash of (URL + extraction parameters)
- **TTL:** 24 hours (configurable via `config.cache_ttl`)
- **Scope:** Fetch results (HTML + screenshots). LLM calls are NOT cached separately — cache invalidation happens at the fetch layer.

## Deduplication

Three levels:
1. **URL dedup** — Discovery deduplicates URLs across probe + DDG results
2. **Asset dedup** — Extraction merges same-named assets from overlapping screenshot tiles
3. **Enrichment dedup** — Drug page search results are deduped by URL before fetching

## Error Handling

| Layer | Strategy |
|-------|----------|
| URL probing | HEAD timeout = 5s, silent failure |
| Page fetch | httpx → Playwright fallback, 15s timeout each |
| LLM extraction | Up to 3 retries with backoff (1s, 3s, 10s) for validation errors |
| Enrichment | Per-asset try/catch, failures return original asset unchanged |
| Company-level | Per-company try/catch in main loop, failures logged and skipped |

## Performance Characteristics

| Operation | Time | Notes |
|-----------|------|-------|
| Direct URL probe | ~1s | 20+ concurrent HEAD requests |
| DDG search fallback | ~3s | 3 parallel queries |
| Page fetch (httpx) | ~2s | Most pages |
| Page fetch (Playwright) | ~5-8s | JS-rendered pages, includes screenshots |
| Text extraction (LLM) | ~3s | Single API call |
| Vision extraction (LLM) | ~8-12s | Multiple screenshot tiles |
| Drug page enrichment | ~15-30s/company | 5 concurrent searches + page fetches |
| **Total per company** | **~30s base** | **~60s with enrichment** |

## Configuration (`config.py`)

| Parameter | Default | Description |
|-----------|---------|-------------|
| `text_threshold` | 500 chars | Min text for HTTP fetch to be usable |
| `vision_threshold` | 300 chars | Below this, force vision-only |
| `hybrid_threshold` | 3000 chars | Below this, take screenshots for hybrid |
| `max_retries` | 3 | LLM extraction retry count |
| `max_drug_pages_per_company` | 50 | Cap on enrichment page fetches |
| `max_enrichment_sources` | 3 | URLs fetched per asset during enrichment |
| `cache_ttl` | 86400s | Cache expiry (24 hours) |
