"""Main entry point for pipeline sourcing system."""
import asyncio
import argparse
from datetime import datetime
from pathlib import Path

from config import config
from models.schema import UserSchema
from services.discovery import discover_pipeline_urls
from services.extraction import extract_pipeline
from services.schema_mapper import map_and_normalize
from services.enrichment import enrich_assets, merge_enrichment_results
from services.export import export_to_excel, export_summary
from utils.fetch import close_browser
from utils.cache import clear_cache


async def process_company(
    company: str,
    schema: UserSchema,
    enrich: bool = True,
    deep_fetch: bool = True,
) -> list[dict]:
    """
    Full pipeline for a single company.

    1. Discover pipeline URLs
    2. Extract assets from pages
    3. Map to schema
    4. Enrich with web search
    """
    print(f"\n{'='*60}")
    print(f"Processing: {company}")
    print("=" * 60)

    # Step 1: Discover URLs
    print("\n[1/4] Discovering pipeline URLs...")
    urls = await discover_pipeline_urls(company)

    if not urls:
        print(f"  No pipeline URLs found for {company}")
        return []

    overview = next((u for u in urls if u.url_type == "overview"), urls[0])
    drug_urls = [u.url for u in urls if u.url_type == "drug_specific"]
    print(f"  Found overview: {overview.url}")
    print(f"  Found {len(drug_urls)} drug-specific pages")

    # Step 2: Extract assets
    print("\n[2/4] Extracting pipeline assets...")
    assets = await extract_pipeline(
        overview.url,
        company,
        drug_urls=drug_urls[:config.max_drug_pages_per_company],
    )
    print(f"  Extracted {len(assets)} assets")

    if not assets:
        return []

    # Step 3: Map to schema
    print("\n[3/4] Mapping to schema...")
    mapped = map_and_normalize(assets, schema)
    print(f"  Mapped {len(mapped)} assets")

    # Step 4: Enrich (optional)
    if enrich:
        print("\n[4/4] Enriching with web search...")
        results = await enrich_assets(mapped, company, deep_fetch=deep_fetch)
        enriched, sources = merge_enrichment_results(results)

        updated_count = sum(1 for r in results if r.updated_fields)
        print(f"  Enriched {len(enriched)} assets ({updated_count} updated)")
        return enriched
    else:
        print("\n[4/4] Skipping enrichment")
        return mapped


async def main(
    companies: list[str],
    output: str = "pipeline_output.xlsx",
    schema_path: str = None,
    enrich: bool = True,
    deep_fetch: bool = True,
    clear: bool = False,
):
    """
    Main pipeline execution.

    Args:
        companies: List of company names to process
        output: Output Excel file path
        schema_path: Path to custom schema JSON (optional)
        enrich: Whether to enrich with web search
        deep_fetch: Whether to deep fetch for gaps
        clear: Whether to clear cache before running
    """
    start_time = datetime.now()
    print(f"\nPipeline Sourcing System v2")
    print(f"Started: {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Companies: {len(companies)}")

    # Clear cache if requested
    if clear:
        count = clear_cache()
        print(f"Cleared {count} cached entries")

    # Load schema
    if schema_path:
        schema = UserSchema.from_json(schema_path)
        print(f"Loaded custom schema from {schema_path}")
    else:
        schema = UserSchema.default()

    # Process each company
    all_assets = []
    results_by_company = {}

    for company in companies:
        try:
            assets = await process_company(
                company,
                schema,
                enrich=enrich,
                deep_fetch=deep_fetch,
            )
            results_by_company[company] = assets
            all_assets.extend(assets)
        except Exception as e:
            print(f"\nError processing {company}: {e}")
            results_by_company[company] = []

    # Clean up browser
    await close_browser()

    # Export results
    print(f"\n{'='*60}")
    print("EXPORT")
    print("=" * 60)

    if all_assets:
        export_to_excel(all_assets, output, schema)

        # Also export summary
        summary_path = output.replace(".xlsx", "_summary.txt")
        export_summary(results_by_company, summary_path)
    else:
        print("No assets to export")

    # Final summary
    end_time = datetime.now()
    duration = (end_time - start_time).total_seconds()

    print(f"\n{'='*60}")
    print("COMPLETE")
    print("=" * 60)
    print(f"Total assets: {len(all_assets)}")
    print(f"Duration: {duration:.1f}s")
    print(f"Output: {output}")


def cli():
    """Command-line interface."""
    parser = argparse.ArgumentParser(
        description="Pipeline Sourcing System - Extract pharma pipeline data"
    )

    parser.add_argument(
        "--company", "-c",
        type=str,
        help="Single company name to process"
    )

    parser.add_argument(
        "--companies", "-C",
        type=str,
        help="Path to file with company names (one per line)"
    )

    parser.add_argument(
        "--output", "-o",
        type=str,
        default="pipeline_output.xlsx",
        help="Output Excel file path (default: pipeline_output.xlsx)"
    )

    parser.add_argument(
        "--schema", "-s",
        type=str,
        help="Path to custom schema JSON file"
    )

    parser.add_argument(
        "--no-enrich",
        action="store_true",
        help="Skip web search enrichment"
    )

    parser.add_argument(
        "--no-deep-fetch",
        action="store_true",
        help="Skip deep fetching for gaps"
    )

    parser.add_argument(
        "--clear-cache",
        action="store_true",
        help="Clear cache before running"
    )

    args = parser.parse_args()

    # Determine companies to process
    companies = []

    if args.company:
        companies = [args.company]
    elif args.companies:
        with open(args.companies) as f:
            companies = [line.strip() for line in f if line.strip()]
    else:
        parser.error("Must specify --company or --companies")

    # Run pipeline
    asyncio.run(main(
        companies=companies,
        output=args.output,
        schema_path=args.schema,
        enrich=not args.no_enrich,
        deep_fetch=not args.no_deep_fetch,
        clear=args.clear_cache,
    ))


if __name__ == "__main__":
    cli()
