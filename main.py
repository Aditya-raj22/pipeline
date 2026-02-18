"""Main entry point for pipeline sourcing system."""
import asyncio
import argparse
import warnings
from datetime import datetime

warnings.filterwarnings("ignore", message=".*coroutine.*was never awaited.*")
warnings.filterwarnings("ignore", message=".*Future exception was never retrieved.*")

from config import config
from models.schema import UserSchema
from services.discovery import discover_pipeline_urls
from services.extraction import extract_pipeline
from services.schema_mapper import map_and_normalize
from services.drug_pages import enrich_from_drug_pages
from services.export import export_to_excel, export_summary
from utils.fetch import close_browser
from utils.cache import clear_cache


async def process_company(
    company: str,
    schema: UserSchema,
    drug_pages: bool = False,
    url: str = None,
) -> list[dict]:
    """Full pipeline for a single company. Returns assets.

    If url is provided, skips discovery and extracts directly from that URL.
    """
    print(f"\n{'='*60}")
    print(f"Processing: {company}")
    print("=" * 60)

    if url:
        # Skip discovery — use provided URL directly
        print(f"\n[1] Using provided URL: {url}")
        overview_url = url
    else:
        # Step 1: Discover overview URL
        print("\n[1] Discovering pipeline URLs...")
        urls = await discover_pipeline_urls(company)

        if not urls:
            print(f"  No pipeline URLs found for {company}")
            return []

        overview = next((u for u in urls if u.url_type == "overview"), urls[0])
        overview_url = overview.url
        print(f"  Found overview: {overview_url}")

    # Step 2: Extract assets from overview
    print("\n[2] Extracting pipeline assets...")
    assets, drug_links = await extract_pipeline(overview_url, company)
    print(f"  Extracted {len(assets)} assets")

    if not assets:
        return []

    # Step 3: Map to schema
    mapped = map_and_normalize(assets, schema)

    # Step 4: Optional drug page enrichment
    if drug_pages:
        print(f"\n[3] Enriching from drug pages...")
        mapped = await enrich_from_drug_pages(mapped, company, overview_links=drug_links)

    return mapped


async def main(
    companies: list[str],
    output: str = "pipeline_output.xlsx",
    schema_path: str = None,
    drug_pages: bool = False,
    clear: bool = False,
):
    """Main pipeline execution."""
    start_time = datetime.now()
    print(f"\nPipeline Sourcing System v3")
    print(f"Started: {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Companies: {len(companies)}")

    if clear:
        count = clear_cache()
        print(f"Cleared {count} cached entries")

    schema = UserSchema.from_json(schema_path) if schema_path else UserSchema.default()

    results_by_company: dict[str, list[dict]] = {}
    all_assets: list[dict] = []

    for company in companies:
        try:
            assets = await process_company(company, schema, drug_pages=drug_pages)
        except Exception as e:
            print(f"\nError processing {company}: {e}")
            assets = []
        results_by_company[company] = assets
        all_assets.extend(assets)

    await close_browser()

    # Export
    print(f"\n{'='*60}")
    print("EXPORT")
    print("=" * 60)

    if all_assets:
        export_to_excel(all_assets, output, schema)
        summary_path = output.replace(".xlsx", "_summary.txt")
        export_summary(results_by_company, summary_path)
    else:
        print("No assets to export")

    duration = (datetime.now() - start_time).total_seconds()
    print(f"\n{'='*60}")
    print(f"COMPLETE — {len(all_assets)} assets in {duration:.1f}s")
    print("=" * 60)


def cli():
    parser = argparse.ArgumentParser(description="Pipeline Sourcing System")
    parser.add_argument("--company", "-c", type=str, help="Single company name")
    parser.add_argument("--companies", "-C", type=str, help="File with company names (one per line)")
    parser.add_argument("--output", "-o", type=str, default="pipeline_output.xlsx")
    parser.add_argument("--schema", "-s", type=str, help="Custom schema JSON file")
    parser.add_argument("--drug-pages", action="store_true", help="Enrich by searching drug pages via DDG")
    parser.add_argument("--clear-cache", action="store_true", help="Clear cache before running")

    args = parser.parse_args()

    companies = []
    if args.company:
        companies = [args.company]
    elif args.companies:
        with open(args.companies) as f:
            companies = [line.strip() for line in f if line.strip()]
    else:
        parser.error("Must specify --company or --companies")

    asyncio.run(main(
        companies=companies,
        output=args.output,
        schema_path=args.schema,
        drug_pages=args.drug_pages,
        clear=args.clear_cache,
    ))


if __name__ == "__main__":
    cli()
