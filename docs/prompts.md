# Build Plan: Pipeline Sourcing System v2

## Guiding Principles

1. **Modular** - Each component independently testable
2. **Incremental** - Build on existing `scrape_pipelines.py`, don't rewrite
3. **Test-first** - Unit tests before integration
4. **Fail gracefully** - Every stage has fallbacks

---

## Interim Schema (until UI ready)

```
Therapeutic Area | Modality | Phase | Asset Name | Description | Therapeutic Target | Indication | Company
```

Hardcoded in `schemas/default.json` - UI will export same format later.

---

## Build Phases

### Phase 1: Foundation (Serper + Models)
**Goal:** Add web search capability and data models

**Tasks:**
1. Create `utils/serper.py` - Serper API client
2. Create `models/` directory with Pydantic schemas
3. Create `config.py` with env loading
4. Test: Mock Serper responses, validate models

**Files:**
```
utils/serper.py
models/__init__.py
models/extracted.py
models/schema.py
config.py
tests/test_serper.py
tests/test_models.py
```

**Test criteria:**
- [ ] Serper client returns parsed organic results
- [ ] Models serialize/deserialize correctly
- [ ] Config loads from .env

---

### Phase 2: URL Discovery
**Goal:** Company name → pipeline URLs

**Tasks:**
1. Create `services/discovery.py`
2. Implement search query construction
3. LLM classification of URL types (overview vs drug-specific)
4. Test with 3 known companies

**Files:**
```
services/discovery.py
tests/test_discovery.py
tests/fixtures/serper_responses/
```

**Test criteria (ground truth companies):**
- [ ] "ABL Bio" returns ablbio.com pipeline page
- [ ] "OliX Pharmaceuticals" returns olixpharma.com pipeline page
- [ ] "Standigm" returns standigm.com pipeline/research page
- [ ] Correctly classifies overview vs drug pages
- [ ] Returns empty list gracefully for unknown company

**Prompts:**
```
DISCOVERY_CLASSIFY_PROMPT = """
Classify these search results for finding pharmaceutical pipeline information.

Company: {company}
Results:
{results}

For each URL, classify as:
- "overview": Main pipeline page listing multiple drugs
- "drug_specific": Page about a single drug/asset
- "news": News article about pipeline
- "irrelevant": Not useful

Return JSON: [{"url": "...", "type": "...", "relevance": 0.0-1.0}]
"""
```

---

### Phase 3: Extraction Refactor
**Goal:** Refactor existing extraction into modular service

**Tasks:**
1. Extract fetch logic from `scrape_pipelines.py` → `utils/fetch.py`
2. Extract cache logic → `utils/cache.py`
3. Create `services/extraction.py` that uses these utils
4. Add drug page link detection
5. Add multi-page merging

**Files:**
```
utils/fetch.py
utils/cache.py
services/extraction.py
tests/test_extraction.py
tests/fixtures/html_samples/
```

**Test criteria:**
- [ ] Existing urls.txt still works
- [ ] Link detection finds drug-specific pages
- [ ] Vision extraction handles phase bar images
- [ ] Multi-page merge deduplicates assets

**Prompts:**
```
EXTRACTION_PROMPT = """
Extract all pharmaceutical pipeline assets from this content.
Source: {url}
Company: {company}

Content:
{content}

For each asset, extract these fields:
- therapeutic_area: e.g., "Oncology", "Neurology", "Ophthalmology", "Dermatology / Fibrosis" (compound areas OK)
- modality: Include delivery route if stated, e.g., "Bispecific Antibody", "GalNAc-asiRNA (subcutaneous)", "cp-asiRNA (intradermal)"
- phase: Use exact value from page. Valid: Preclinical, Phase 1, Phase 1/2, Phase 2, Phase 2/3, Phase 3, Filed, Approved, IND enabling study, Phase 1 completed, Discovery, Platform
- asset_name: Drug/compound code (e.g., "ABL001", "OLX10212") or name
- description: Mechanism of action or brief summary
- therapeutic_target: Molecular target (e.g., "VEGF/DLL4", "PD-L1/4-1BB", "CTGF")
- indication: Disease/condition (e.g., "Solid tumors", "Wet and dry AMD", "Parkinson's disease")

If partner/licensee mentioned, append to company field: "{company} + {partner}"

Return JSON array. Use null for truly unknown fields (will become "Undisclosed").
"""

VISION_EXTRACTION_PROMPT = """
Extract pharmaceutical pipeline data from this image.

The image may contain:
- Tables with drug information
- Visual phase indicators (colored bars showing development stage)
- Pipeline charts or diagrams

For visual phase indicators:
- Solid filled section = completed
- Partial fill or current marker = ongoing
- Map to: Preclinical, Phase 1, Phase 1/2, Phase 2, Phase 2/3, Phase 3, Filed, Approved
- If bar shows progress within a phase, note as "Phase X (ongoing)"

Extract ALL assets visible. Return JSON matching schema.
"""

LINK_DETECTION_PROMPT = """
Find links to individual drug/asset pages from this pipeline overview page.

Page URL: {url}
Links found on page:
{links}

Return only links that lead to detailed information about specific drugs/assets.
Exclude: news, careers, contact, general research pages.

Return JSON: [{"url": "...", "asset_name": "..."}]
"""
```

---

### Phase 4: Schema Mapping
**Goal:** Map extracted data to user-defined schema

**Tasks:**
1. Create `models/schema.py` with UserSchema
2. Create `services/schema_mapper.py`
3. Create `schemas/default.json`
4. Implement fuzzy field matching using aliases
5. Test with various schema configs

**Files:**
```
services/schema_mapper.py
schemas/default.json
tests/test_schema_mapper.py
```

**Test criteria:**
- [ ] Exact field names map correctly
- [ ] Aliases work ("drug" → "Asset Name")
- [ ] Missing required fields get "undisclosed"
- [ ] Extra extracted fields preserved in metadata

---

### Phase 5: Enrichment
**Goal:** Enhance each asset with web search

**Tasks:**
1. Create `services/enrichment.py`
2. Implement snippet-based synthesis (Tier 1)
3. Implement deep fetch logic (Tier 2)
4. Implement gap detection
5. Test with known assets

**Files:**
```
services/enrichment.py
tests/test_enrichment.py
```

**Test criteria:**
- [ ] Finds recent news for known drugs
- [ ] Updates phase if web shows advancement
- [ ] Deep fetch triggers only when needed
- [ ] Citations properly tracked

**Prompts:**
```
ENRICHMENT_SYNTHESIS_PROMPT = """
You are updating pharmaceutical asset data with the latest web information.

Asset: {asset_name}
Company: {company}

Current data:
{current_data}

Latest web search results:
{search_results}

Instructions:
1. If web results contain NEWER information (e.g., phase advancement, new indication), update the field
2. If web results CONTRADICT current data, prefer more recent/authoritative source
3. If web results ADD information for empty/"undisclosed" fields, fill them in
4. Keep current values if web results don't provide better information
5. For "Latest Update" field, summarize the most significant recent news

Return updated JSON. Only change fields where you have confident new information.
"""

DEEP_FETCH_PROMPT = """
Extract specific information about {asset_name} from this page.

Looking for: {field_name}
Company: {company}

Page content:
{content}

If the {field_name} is clearly stated, return it.
If ambiguous or not found, return null.
Do not guess or infer.
"""

GAP_DETECTION_PROMPT = """
Analyze these search snippets for {asset_name}.

Snippets:
{snippets}

Required fields still missing: {missing_fields}

For each missing field, does ANY snippet contain explicit information?
Return JSON: {{"field_name": true/false, ...}}

Only return true if the information is EXPLICITLY stated, not implied.
"""
```

---

### Phase 6: Integration + Export
**Goal:** Wire everything together

**Tasks:**
1. Create `main.py` entry point
2. Create `services/export.py`
3. Implement full pipeline orchestration
4. Add CLI arguments
5. Integration testing

**Files:**
```
main.py
services/export.py
tests/test_integration.py
```

**Test criteria:**
- [ ] Single company E2E works
- [ ] Batch of 5 companies works
- [ ] Excel output has all columns
- [ ] Sources column populated
- [ ] Handles errors gracefully

**CLI:**
```bash
# Basic usage
python main.py --company "Pfizer"

# Batch mode
python main.py --companies companies.txt

# Custom schema
python main.py --company "Pfizer" --schema custom_schema.json

# Output location
python main.py --company "Pfizer" --output results/pfizer.xlsx
```

---

## Testing Strategy

### Unit Tests
```
tests/
├── test_serper.py          # Mock API responses
├── test_models.py          # Pydantic validation
├── test_discovery.py       # URL classification
├── test_extraction.py      # HTML/vision parsing
├── test_schema_mapper.py   # Field mapping
├── test_enrichment.py      # Synthesis logic
└── fixtures/
    ├── serper_responses/   # Sample API responses
    ├── html_samples/       # Real pipeline page HTML
    ├── screenshots/        # Pipeline images for vision tests
    └── schemas/            # Test schema configs
```

### Integration Tests
```
tests/test_integration.py
- test_single_company_e2e()
- test_batch_companies()
- test_with_custom_schema()
- test_error_recovery()
```

### Ground Truth Validation
Reference file: `sourcing_ground_truth.xlsx`

| Company | Expected Assets | Key Validation Points |
|---------|-----------------|----------------------|
| ABL Bio | 14 | ABL001 (Phase 3), ABL301 (Neurology), bispecific antibodies |
| OliX Pharmaceuticals | 16 | asiRNA modalities with routes, OLX75016 licensed to Eli Lilly |
| Standigm | 7 | Mostly "Discovery"/"Undisclosed", AI platform tools |

**Per-company acceptance criteria:**
- [ ] ABL Bio: ≥10 assets, ABL001/ABL105/ABL301 present, correct phases
- [ ] OliX: ≥12 assets, modality includes delivery route (e.g., "GalNAc-asiRNA (subcutaneous)")
- [ ] Standigm: Handle gracefully, detect platform entry, don't fail on mostly-undisclosed

**Field accuracy checks:**
- [ ] Phase matches ground truth (most critical)
- [ ] Target format matches (e.g., "VEGF/DLL4" not "VEGF and DLL4")
- [ ] Partner appended to Company when present

---

## Rollout Order

```
Week 1: Phases 1-2 (Foundation + Discovery)
        Deliverable: Can find pipeline URLs for any company

Week 2: Phase 3 (Extraction Refactor)
        Deliverable: Modular extraction with multi-page support

Week 3: Phases 4-5 (Schema + Enrichment)
        Deliverable: Full data flow with enrichment

Week 4: Phase 6 (Integration)
        Deliverable: Production-ready CLI
```

---

## Risk Mitigation

| Risk | Mitigation |
|------|------------|
| Serper results low quality | Fall back to broader queries, try company domain filter |
| Pipeline page blocks scraping | Playwright stealth mode, rotate user agents |
| Vision model misreads phase bars | Prompt engineering with examples, human verification flag |
| Enrichment adds wrong info | Confidence scoring, require explicit source match |
| Schema mapping ambiguous | LLM-assisted field matching as fallback |

---

## Success Metrics (vs Ground Truth)

1. **Asset coverage**: ≥70% of ground truth assets extracted (some may be sunset)
2. **Asset name accuracy**: 100% exact match for extracted assets
3. **Phase accuracy**: ≥85% match ground truth
4. **Field completeness**: ≤20% "Undisclosed" for non-Standigm companies
5. **Enrichment value**: ≥30% of assets get additional info from web search
6. **Cost**: <$0.15 per company

---

## Next Steps

1. Confirm this plan aligns with your vision
2. Set up Serper API key in `.env`
3. Start Phase 1: I'll build `utils/serper.py` and models
4. Run first test with 3 companies to validate approach
