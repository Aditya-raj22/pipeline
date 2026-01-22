# Technical Specification: Pipeline Sourcing System v2

## Overview

Automated pharmaceutical pipeline discovery and enrichment system that:
1. Takes company name → discovers pipeline URLs via web search
2. Extracts assets from pipeline pages (tables, images, text)
3. Maps to user-defined schema
4. Enriches each asset with latest web intelligence
5. Exports to Excel with source citations

---

## Architecture

```
┌─────────────────┐
│  Company Name   │
└────────┬────────┘
         ▼
┌─────────────────────────────────────┐
│  Stage 1: URL Discovery (Serper)    │
│  "{company} pharmaceutical pipeline"│
└────────┬────────────────────────────┘
         ▼
┌─────────────────────────────────────┐
│  Stage 2: Pipeline Extraction       │
│  - Fetch (HTTP → Playwright → Vision)│
│  - LLM parse to base schema         │
│  - Detect drug-specific page links  │
└────────┬────────────────────────────┘
         ▼
┌─────────────────────────────────────┐
│  Stage 3: Schema Mapping            │
│  - Load user schema from JSON       │
│  - Map extracted fields             │
│  - Mark unmapped as "undisclosed"   │
└────────┬────────────────────────────┘
         ▼
┌─────────────────────────────────────┐
│  Stage 4: Asset Enrichment (per asset)│
│  - Serper: "{asset} {company} latest"│
│  - Snippet synthesis via LLM        │
│  - Conditional deep fetch if gaps   │
│  - Update schema + store citations  │
└────────┬────────────────────────────┘
         ▼
┌─────────────────────────────────────┐
│  Stage 5: Export                    │
│  - Excel with all fields            │
│  - Source URLs column               │
└─────────────────────────────────────┘
```

---

## Data Models

### Base Extraction Schema (internal)

```python
class ExtractedAsset(BaseModel):
    """Raw extraction before user schema mapping"""
    asset_name: str
    phase: str | None
    indication: str | None
    modality: str | None
    therapeutic_area: str | None
    target: str | None
    description: str | None
    partner: str | None
    # Metadata
    source_url: str
    extraction_method: Literal["text", "vision", "hybrid"]
```

### User Schema (loaded from JSON)

```python
class UserSchemaField(BaseModel):
    name: str                    # Column name
    type: Literal["text", "phase", "date", "url", "list"]
    required: bool = False
    aliases: list[str] = []     # Alternative field names to match
    default: str = "undisclosed"

class UserSchema(BaseModel):
    fields: list[UserSchemaField]
```

### Interim Schema (hardcoded until UI ready)

Column order: `Therapeutic Area | Modality | Phase | Asset Name | Description | Therapeutic Target | Indication | Company`

```json
{
  "fields": [
    {"name": "Therapeutic Area", "type": "text", "aliases": ["area", "therapy area", "disease area"]},
    {"name": "Modality", "type": "text", "aliases": ["platform", "technology", "drug type"]},
    {"name": "Phase", "type": "phase", "aliases": ["stage", "development phase", "clinical stage"]},
    {"name": "Asset Name", "type": "text", "required": true, "aliases": ["drug", "compound", "candidate", "program"]},
    {"name": "Description", "type": "text", "aliases": ["summary", "mechanism", "moa"]},
    {"name": "Therapeutic Target", "type": "text", "aliases": ["target", "molecular target"]},
    {"name": "Indication", "type": "text", "aliases": ["disease", "condition"]},
    {"name": "Company", "type": "text", "aliases": ["sponsor", "developer"]}
  ]
}
```

**Phase normalization rules** (from ground truth):
- Standard: `Preclinical`, `Phase 1`, `Phase 1/2`, `Phase 2`, `Phase 2/3`, `Phase 3`, `Filed`, `Approved`
- Allow: `IND enabling study`, `Phase 1 completed`, `Discovery`, `Platform`
- Partner in Company: `"OliX Pharmaceuticals + Eli Lilly"` format is valid

### Enriched Asset (final output)

```python
class EnrichedAsset(BaseModel):
    # All user schema fields dynamically mapped
    # Plus:
    company: str
    sources: list[str]          # URLs used for enrichment
    last_updated: str           # ISO date
    confidence: Literal["high", "medium", "low"]
```

---

## Component Specifications

### 1. URL Discovery (`discovery.py`)

**Input:** Company name (str)
**Output:** List of pipeline URLs with classification

```python
@dataclass
class DiscoveredURL:
    url: str
    title: str
    snippet: str
    url_type: Literal["overview", "drug_specific", "news", "unknown"]
    relevance_score: float  # 0-1
```

**Logic:**
1. Query Serper: `"{company}" pharmaceutical pipeline site:{company_domain} OR pipeline`
2. Also query: `"{company}" drug candidates clinical trials`
3. LLM classifies each result as overview/drug_specific/news
4. Return top 3 pipeline URLs, sorted by relevance

**Serper API:**
```python
async def search_serper(query: str, num_results: int = 10) -> list[dict]:
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://google.serper.dev/search",
            headers={"X-API-KEY": SERPER_API_KEY},
            json={"q": query, "num": num_results}
        )
        return resp.json().get("organic", [])
```

---

### 2. Pipeline Extraction (`extraction.py`)

**Extends existing `scrape_pipelines.py`**

New capabilities:
- **Link detection**: Find drug-specific page links on overview page
- **Multi-page crawl**: Fetch overview + up to 5 drug pages
- **Merge logic**: Combine data from multiple pages per asset

```python
async def extract_pipeline(urls: list[DiscoveredURL], company: str) -> list[ExtractedAsset]:
    # 1. Fetch overview page
    overview = await fetch_with_fallback(urls[0].url)

    # 2. Extract assets + detect drug page links
    assets, drug_links = await extract_assets_and_links(overview)

    # 3. Fetch drug-specific pages (parallel, max 5)
    drug_pages = await asyncio.gather(*[
        fetch_with_fallback(link) for link in drug_links[:5]
    ])

    # 4. Enrich assets with drug page data
    for page in drug_pages:
        detail = await extract_drug_detail(page)
        merge_into_asset(assets, detail)

    return assets
```

**Vision extraction prompt (for phase bars):**
```
Extract pharmaceutical pipeline data from this image.
For visual phase indicators (colored bars, progress indicators):
- Map to: Preclinical, Phase 1, Phase 1/2, Phase 2, Phase 2/3, Phase 3, Filed, Approved
- If bar shows partial progress within a phase, note as "Phase X (ongoing)"
Return JSON matching the schema.
```

---

### 3. Schema Mapper (`schema_mapper.py`)

```python
def map_to_user_schema(
    extracted: list[ExtractedAsset],
    user_schema: UserSchema
) -> list[dict]:
    """
    Maps extracted assets to user-defined schema.
    Uses field aliases for fuzzy matching.
    Fills missing required fields with 'undisclosed'.
    """
    results = []
    for asset in extracted:
        row = {}
        asset_dict = asset.model_dump()

        for field in user_schema.fields:
            # Try exact match, then aliases
            value = None
            candidates = [field.name.lower()] + [a.lower() for a in field.aliases]

            for key, val in asset_dict.items():
                if key.lower() in candidates or any(c in key.lower() for c in candidates):
                    value = val
                    break

            row[field.name] = value if value else field.default

        results.append(row)
    return results
```

---

### 4. Asset Enrichment (`enrichment.py`)

**Two-tier approach:**

**Tier 1: Snippet Synthesis**
```python
async def enrich_from_snippets(
    asset: dict,
    company: str,
    schema: UserSchema
) -> tuple[dict, list[str]]:
    # Search for latest news
    query = f"{asset['Asset Name']} {company} clinical trial latest 2024 2025"
    results = await search_serper(query, num_results=3)

    # Combine snippets
    context = "\n".join([
        f"[{r['title']}]: {r['snippet']}"
        for r in results
    ])

    # LLM synthesis
    prompt = f"""
    Current data for {asset['Asset Name']}:
    {json.dumps(asset, indent=2)}

    Latest web results:
    {context}

    Update any fields with newer/better information.
    Keep existing values if web results don't contradict.
    Return updated JSON matching schema.
    """

    updated = await llm_extract(prompt, schema)
    urls = [r["link"] for r in results]
    return updated, urls
```

**Tier 2: Deep Fetch (conditional)**
```python
async def needs_deep_fetch(asset: dict, schema: UserSchema) -> list[str]:
    """Returns list of fields that need more data"""
    gaps = []
    for field in schema.fields:
        if field.required and asset.get(field.name) in [None, "undisclosed", ""]:
            gaps.append(field.name)
        # Also flag ambiguous values
        if asset.get(field.name) and "phase" in field.name.lower():
            if not re.match(r"Phase \d|Preclinical|Filed|Approved", asset[field.name]):
                gaps.append(field.name)
    return gaps

async def deep_fetch_for_field(
    asset: dict,
    company: str,
    field: str,
    serper_results: list[dict]
) -> str | None:
    """Fetch full page content for specific field extraction"""
    for result in serper_results:
        content = await fetch_with_fallback(result["link"])
        extracted = await llm_extract_field(content, asset["Asset Name"], field)
        if extracted and extracted != "undisclosed":
            return extracted
    return None
```

---

### 5. Export (`export.py`)

```python
def export_to_excel(
    assets: list[dict],
    schema: UserSchema,
    output_path: str = "pipeline_output.xlsx"
):
    # Build DataFrame with schema column order
    columns = [f.name for f in schema.fields] + ["Company", "Sources", "Last Updated"]
    df = pd.DataFrame(assets)
    df = df.reindex(columns=columns, fill_value="undisclosed")

    # Style phase column with colors
    # ... (conditional formatting)

    df.to_excel(output_path, index=False)
```

---

## API Costs Estimate

| Operation | Cost | Per Company (est.) |
|-----------|------|-------------------|
| Serper search | $0.001/query | ~$0.005 (5 queries) |
| GPT-4.1-nano (text) | $0.10/1M tokens | ~$0.01 |
| GPT-4.1 (vision) | $2.50/1M tokens | ~$0.05 (if needed) |
| Deep fetch | $0.001 + tokens | ~$0.02 (if needed) |
| **Total per company** | | **~$0.05-0.10** |

---

## Error Handling

| Scenario | Handling |
|----------|----------|
| No pipeline URL found | Return empty + log warning |
| Page blocked/403 | Try Playwright with stealth |
| Vision extraction fails | Fall back to text-only |
| Serper rate limit | Exponential backoff, max 3 retries |
| LLM validation fails | Retry with error feedback (existing) |
| Schema field unmappable | Set to "undisclosed" |

---

## Configuration

```python
# config.py
@dataclass
class Config:
    serper_api_key: str
    openai_api_key: str
    max_concurrent_companies: int = 3
    max_concurrent_fetches: int = 5
    max_drug_pages_per_company: int = 5
    max_enrichment_sources: int = 3
    cache_ttl: int = 86400
    deep_fetch_threshold: float = 0.5  # Confidence below this triggers deep fetch
```

---

## Ground Truth Test Companies

Validation against `sourcing_ground_truth.xlsx`:

| Company | Assets | Key Patterns |
|---------|--------|--------------|
| **ABL Bio** | 14 | Bispecific antibodies, 4-1BB platform, phases Preclinical→Phase 3 |
| **OliX Pharmaceuticals** | 16 | asiRNA modalities with delivery routes (intravitreal, subcutaneous), partners in Company field |
| **Standigm** | 7 | AI drug discovery platform, mostly "Discovery" phase, many "Undisclosed" fields expected |

**Edge cases from ground truth:**
1. Compound modality: `"GalNAc-asiRNA (subcutaneous)"`, `"cp-asiRNA (intradermal)"`
2. Compound therapeutic area: `"Dermatology / Fibrosis"`, `"Hepatology / Metabolic"`
3. Partner in Company: `"OliX Pharmaceuticals + Laboratoires Théa (returned rights)"`
4. Non-standard phases: `"IND enabling study"`, `"Phase 1 completed"`, `"Discovery"`
5. Platform entries: Standigm's AI SaaS tools (ASK-LLM, STELLA, etc.)
6. Sunset assets: Some ground truth assets may no longer appear on current pipeline pages

**Success criteria:**
- ABL Bio: Extract ≥10 assets, match asset names (ABL001, ABL105, ABL301, etc.)
- OliX: Extract ≥12 assets, correctly parse asiRNA modality variants
- Standigm: Handle mostly-undisclosed gracefully, detect platform entry

---

## File Structure (proposed)

```
sourcing/
├── scrape_pipelines.py      # Keep for backwards compat
├── main.py                  # New entry point
├── config.py                # Configuration
├── models/
│   ├── __init__.py
│   ├── extracted.py         # ExtractedAsset
│   ├── schema.py            # UserSchema, UserSchemaField
│   └── enriched.py          # EnrichedAsset
├── services/
│   ├── __init__.py
│   ├── discovery.py         # URL discovery via Serper
│   ├── extraction.py        # Pipeline extraction (extends existing)
│   ├── schema_mapper.py     # Schema mapping
│   ├── enrichment.py        # Asset enrichment
│   └── export.py            # Excel export
├── utils/
│   ├── __init__.py
│   ├── fetch.py             # HTTP/Playwright/Vision fetch (from existing)
│   ├── cache.py             # Caching (from existing)
│   └── serper.py            # Serper API client
├── schemas/
│   └── default.json         # Default user schema
├── tests/
│   ├── test_discovery.py
│   ├── test_extraction.py
│   ├── test_enrichment.py
│   └── fixtures/            # Sample HTML, images for testing
└── docs/
    ├── technical_spec.md
    └── prompts.md
```
