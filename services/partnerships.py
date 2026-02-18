"""Partnership enrichment - find licensing/collaboration deals for drugs."""
import asyncio
import json
from dataclasses import dataclass
from typing import Optional
from openai import AsyncOpenAI
from config import config
from utils.serper import search_serper

client = AsyncOpenAI(api_key=config.openai_api_key)


@dataclass
class Partnership:
    """Partnership/licensing deal for a drug."""
    partner: str
    deal_date: str  # e.g., "Oct 2022"
    status: str  # "Active", "Returned", "Terminated"
    deal_value: str  # Optional - total deal value if found
    source: str  # "pipeline" or "search"


EXTRACT_PROMPT = """Extract partnership/licensing deal information for {drug_name} ({company}) from these search results.

Search results:
{results}

Look for:
- Licensing deals, collaborations, partnerships
- Partner company name
- Deal announcement date
- Whether deal is active or if rights were returned/terminated

Return JSON:
{{
  "has_partnership": true/false,
  "partner": "Partner company name" or null,
  "deal_date": "Month Year" (e.g., "Oct 2022") or null,
  "status": "Active" or "Returned" or "Terminated" or null,
  "deal_value": "Total deal value if mentioned" or null
}}

If no partnership found, return {{"has_partnership": false}}"""


async def search_partnership(
    drug_name: str,
    company: str,
) -> Optional[Partnership]:
    """
    Search for partnership/licensing deals for a drug.

    Uses one Serper call to find deal info.
    """
    # Targeted search query
    query = f'"{drug_name}" "{company}" partnership OR license OR deal OR collaboration'

    try:
        results = await search_serper(query, num_results=5)
    except Exception as e:
        return None

    if not results:
        return None

    # Format for LLM
    results_text = "\n\n".join([
        f"[{r.title}]\n{r.snippet}"
        for r in results
    ])

    prompt = EXTRACT_PROMPT.format(
        drug_name=drug_name,
        company=company,
        results=results_text,
    )

    try:
        response = await client.chat.completions.create(
            model=config.text_model,
            messages=[
                {"role": "system", "content": "Extract partnership deal info. Return valid JSON only."},
                {"role": "user", "content": prompt}
            ],
            response_format={"type": "json_object"}
        )

        data = json.loads(response.choices[0].message.content)

        if not data.get("has_partnership"):
            return None

        return Partnership(
            partner=data.get("partner", ""),
            deal_date=data.get("deal_date", ""),
            status=data.get("status", "Active"),
            deal_value=data.get("deal_value", ""),
            source="search",
        )
    except Exception as e:
        return None


def check_existing_partnership(asset: dict) -> Optional[Partnership]:
    """
    Check if partnership info already exists in scraped asset data.

    Looks for partner mentions in description or dedicated fields.
    """
    description = asset.get("Description", "").lower()

    # Common partner indicators
    partner_keywords = [
        "licensed to", "partnered with", "collaboration with",
        "in partnership with", "out-licensed to", "deal with",
    ]

    # Check if any partner keyword is in description
    has_partner_mention = any(kw in description for kw in partner_keywords)

    # Also check if there's a dedicated Partner field from extraction
    existing_partner = asset.get("Partner", "")

    if existing_partner:
        return Partnership(
            partner=existing_partner,
            deal_date=asset.get("Deal Date", ""),
            status=asset.get("Partnership Status", "Active"),
            deal_value="",
            source="pipeline",
        )

    # If partner mentioned in description, we should search for details
    if has_partner_mention:
        return None  # Signal to search for more details

    return None


async def enrich_asset_partnership(
    asset: dict,
    company: str,
) -> Optional[Partnership]:
    """
    Get partnership info for an asset.

    1. Check if already in scraped data
    2. If not (or incomplete), do one Serper search
    """
    drug_name = asset.get("Asset Name", "")

    if not drug_name or drug_name == "Undisclosed":
        return None

    # Check existing data first
    existing = check_existing_partnership(asset)

    if existing and existing.partner:
        # Already have partner info, but verify/enrich with search
        partnership = await search_partnership(drug_name, company)
        if partnership:
            # Merge - prefer search data but keep existing if search incomplete
            return Partnership(
                partner=partnership.partner or existing.partner,
                deal_date=partnership.deal_date or existing.deal_date,
                status=partnership.status or existing.status,
                deal_value=partnership.deal_value,
                source="search",
            )
        return existing

    # No existing data - search
    return await search_partnership(drug_name, company)


async def enrich_assets_partnerships(
    assets: list[dict],
    company: str,
    max_concurrent: int = 3,
) -> list[tuple[dict, Optional[Partnership]]]:
    """
    Enrich multiple assets with partnership data.
    """
    semaphore = asyncio.Semaphore(max_concurrent)

    async def enrich_one(asset):
        async with semaphore:
            partnership = await enrich_asset_partnership(asset, company)
            return asset, partnership

    tasks = [enrich_one(asset) for asset in assets]
    return await asyncio.gather(*tasks)


def apply_partnership_enrichment(
    asset: dict,
    partnership: Optional[Partnership],
) -> dict:
    """
    Apply partnership data to asset.
    """
    updated = asset.copy()

    if partnership:
        updated["Partner"] = partnership.partner
        updated["Deal Date"] = partnership.deal_date
        updated["Deal Status"] = partnership.status
        if partnership.deal_value:
            updated["Deal Value"] = partnership.deal_value
    else:
        # No partnership found
        updated["Partner"] = ""
        updated["Deal Date"] = ""
        updated["Deal Status"] = ""

    return updated


# Sync wrapper for testing
def search_partnership_sync(drug_name: str, company: str) -> dict:
    """Test partnership search for a drug."""
    async def run():
        partnership = await search_partnership(drug_name, company)
        if partnership:
            return {
                "partner": partnership.partner,
                "deal_date": partnership.deal_date,
                "status": partnership.status,
                "deal_value": partnership.deal_value,
            }
        return {"partner": None}
    return asyncio.run(run())


if __name__ == "__main__":
    import sys
    drug = sys.argv[1] if len(sys.argv) > 1 else "ABL301"
    company = sys.argv[2] if len(sys.argv) > 2 else "ABL Bio"

    print(f"Searching partnership for: {drug} ({company})")
    result = search_partnership_sync(drug, company)

    if result.get("partner"):
        print(f"\nPartner: {result['partner']}")
        print(f"Deal Date: {result['deal_date']}")
        print(f"Status: {result['status']}")
        if result.get('deal_value'):
            print(f"Deal Value: {result['deal_value']}")
    else:
        print("\nNo partnership found")
