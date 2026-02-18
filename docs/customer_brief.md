# Pipeline Sourcer — Product Brief

## What It Does

Pipeline Sourcer automatically discovers and extracts pharmaceutical pipeline data for any biotech or pharma company. Given a list of company names (and optionally their pipeline page URLs), it returns a structured Excel file with every drug asset, its development phase, therapeutic area, target, indication, and more.

## How It Works

The system runs a 4-step pipeline for each company:

1. **Discover** — Probes common pipeline page URL patterns (e.g., `company.com/pipeline`). If no direct hit, falls back to DuckDuckGo search to find the best pipeline overview page.

2. **Extract** — Fetches the pipeline page and extracts drug assets using AI. Works on both text-rich HTML tables and graphical pipeline charts (via screenshot analysis).

3. **Normalize** — Maps extracted data to a consistent 9-column schema, deduplicates entries, and fills in defaults.

4. **Enrich** *(optional)* — For each asset with missing data (indication, target, description), searches the web for drug-specific pages and uses AI to fill gaps.

## Output Schema

Each row in the output represents one drug asset:

| Column | Description | Example |
|--------|-------------|---------|
| Asset Name | Drug code or compound name | ABL001, Lazertinib |
| Phase | Development stage | Preclinical, Phase 1, Phase 2, Phase 3, Filed, Approved |
| Therapeutic Area | Disease category | Oncology, Neurology, Immunology |
| Modality | Drug type/platform | Small molecule, Bispecific Antibody, ADC |
| Indication | Disease(s) being treated | NSCLC, AML, Parkinson's disease |
| Therapeutic Target | Molecular target | EGFR, CD33, PD-L1 |
| Description | Mechanism of action summary | Selective EGFR inhibitor |
| Company | Sponsor/developer | ABL Bio |
| Sources | URLs where data was sourced | Pipeline page URL(s) |

## Accuracy & Limitations

- **Best for companies with dedicated pipeline pages** — structured tables and charts yield the highest extraction accuracy.
- **Graphical pipelines** (hexagons, bar charts) are handled via screenshot analysis, which may occasionally miss assets in unusual visual layouts.
- **Enrichment improves completeness** — enabling drug page enrichment typically fills 60-80% of missing indications and targets, but adds ~30s per company.
- **Phase normalization** — phases are standardized to: Preclinical, IND-enabling, Phase 1, Phase 1/2, Phase 2, Phase 3, Filed, Approved. Non-standard phases are mapped to the closest match.
- **Deduplication** — the same drug appearing in multiple sections or with slightly different names is merged into a single entry.
- **"Undisclosed" values** — when data cannot be determined from available sources, fields show "Undisclosed" rather than guessing.

## Performance

| Metric | Without Enrichment | With Enrichment |
|--------|-------------------|-----------------|
| Time per company | ~30 seconds | ~60 seconds |
| 500 companies | ~4 hours | ~8 hours |

Processing is serial to maintain browser session stability. Results can be downloaded at any time during a run (partial results are available).
