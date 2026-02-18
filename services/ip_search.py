"""IP search service - find patents via Google Patents."""
import asyncio
import re
import json
from dataclasses import dataclass
from openai import AsyncOpenAI
from config import config
from utils.serper import search_serper, SerperResult

client = AsyncOpenAI(api_key=config.openai_api_key)


@dataclass
class Patent:
    """Extracted patent information."""
    patent_number: str
    title: str
    targets: list[str]  # e.g., ["VEGF", "DLL4"]
    therapeutic_area: str  # e.g., "Oncology", "Neurology"
    assignee: str
    filing_date: str
    status: str  # "Active", "Ceased", "Pending"
    url: str

    def to_dict(self) -> dict:
        return {
            "Patent Number": self.patent_number,
            "Title": self.title,
            "Targets": ", ".join(self.targets),
            "Therapeutic Area": self.therapeutic_area,
            "Assignee": self.assignee,
            "Filing Date": self.filing_date,
            "Status": self.status,
            "URL": self.url,
        }


EXTRACT_PROMPT = """Extract patent information from these Google Patents search results for {company}.

Search results:
{results}

For each patent found, extract:
- patent_number: The patent/application number (e.g., WO2022039490A1, US20210355202A1)
- title: Patent title
- targets: List of molecular targets mentioned (e.g., ["PD-L1", "4-1BB"], ["alpha-synuclein", "IGF1R"])
- therapeutic_area: "Oncology", "Neurology", "Immunology", or other area
- assignee: Company name
- filing_date: Filing date if mentioned
- status: "Active", "Ceased", "Pending", or "Unknown"

Return JSON: {{"patents": [...]}}
Only include patents where {company} is the assignee/applicant. Skip unrelated results."""


async def search_company_patents(
    company: str,
    korean_name: str = None,
    max_results: int = 20,
) -> list[Patent]:
    """
    Search Google Patents for a company's IP.

    Args:
        company: Company name (English)
        korean_name: Korean company name for better coverage
        max_results: Max patents to return

    Returns:
        List of Patent objects
    """
    # Build search queries
    queries = [
        f'site:patents.google.com "{company}" assignee',
        f'site:patents.google.com "{company}" applicant patent',
    ]
    if korean_name:
        queries.append(f'site:patents.google.com "{korean_name}" assignee')

    # Run searches in parallel
    tasks = [search_serper(q, num_results=10) for q in queries]
    results_lists = await asyncio.gather(*tasks, return_exceptions=True)

    # Flatten and dedupe by URL
    seen_urls = set()
    all_results: list[SerperResult] = []

    for results in results_lists:
        if isinstance(results, Exception):
            continue
        for r in results:
            # Only keep Google Patents URLs
            if "patents.google.com" in r.link and r.link not in seen_urls:
                seen_urls.add(r.link)
                all_results.append(r)

    if not all_results:
        return []

    # Format for LLM extraction
    results_text = "\n\n".join([
        f"[{i+1}] {r.title}\nURL: {r.link}\n{r.snippet}"
        for i, r in enumerate(all_results[:max_results])
    ])

    prompt = EXTRACT_PROMPT.format(company=company, results=results_text)

    try:
        response = await client.chat.completions.create(
            model=config.text_model,
            messages=[
                {"role": "system", "content": "Extract patent data from search results. Return valid JSON."},
                {"role": "user", "content": prompt}
            ],
            response_format={"type": "json_object"}
        )

        data = json.loads(response.choices[0].message.content)
        patents_data = data.get("patents", [])

        # Build Patent objects
        patents = []
        for p in patents_data:
            patents.append(Patent(
                patent_number=p.get("patent_number", ""),
                title=p.get("title", ""),
                targets=p.get("targets", []),
                therapeutic_area=p.get("therapeutic_area", "Unknown"),
                assignee=p.get("assignee", company),
                filing_date=p.get("filing_date", ""),
                status=p.get("status", "Unknown"),
                url=p.get("url", ""),
            ))

        return patents

    except Exception as e:
        print(f"  Patent extraction failed: {e}")
        return []


def extract_targets_from_title(title: str) -> list[str]:
    """Quick regex extraction of targets from patent title."""
    # Common patterns: "Anti-X/Anti-Y", "X x Y", "targeting X and Y"
    targets = []

    # Pattern: anti-X/anti-Y
    anti_pattern = re.findall(r'anti-([A-Z0-9-]+)', title, re.IGNORECASE)
    targets.extend(anti_pattern)

    # Pattern: X/Y or X x Y
    combo_pattern = re.findall(r'([A-Z][A-Z0-9-]+)\s*[/x×]\s*([A-Z][A-Z0-9-]+)', title, re.IGNORECASE)
    for match in combo_pattern:
        targets.extend(match)

    # Dedupe and clean
    return list(set(t.upper().strip() for t in targets if len(t) > 1))


async def find_ip_not_on_pipeline(
    company: str,
    pipeline_assets: list[dict],
    korean_name: str = None,
) -> list[Patent]:
    """
    Find patents that may not be represented in the pipeline.

    Args:
        company: Company name
        pipeline_assets: List of pipeline assets (with "Therapeutic Target" field)
        korean_name: Korean company name

    Returns:
        List of patents with targets not in pipeline
    """
    # Get all patents
    patents = await search_company_patents(company, korean_name)

    if not patents:
        return []

    # Extract known targets from pipeline
    known_targets = set()
    for asset in pipeline_assets:
        target = asset.get("Therapeutic Target", "")
        if target and target != "Undisclosed":
            # Split multi-target (e.g., "VEGF x DLL4")
            for t in re.split(r'[/x×,\s]+', target):
                if len(t) > 1:
                    known_targets.add(t.upper().strip())

    # Find patents with novel targets
    novel_patents = []
    for patent in patents:
        patent_targets = set(t.upper() for t in patent.targets)

        # Check if any target is not in known pipeline
        if patent_targets and not patent_targets.issubset(known_targets):
            novel_patents.append(patent)

    return novel_patents


# Sync wrapper for testing
def search_patents_sync(company: str, korean_name: str = None) -> list[dict]:
    """Synchronous wrapper for testing."""
    patents = asyncio.run(search_company_patents(company, korean_name))
    return [p.to_dict() for p in patents]


if __name__ == "__main__":
    # Quick test
    import sys
    company = sys.argv[1] if len(sys.argv) > 1 else "ABL Bio"
    korean = sys.argv[2] if len(sys.argv) > 2 else "에이비엘바이오"

    print(f"Searching patents for: {company}")
    patents = search_patents_sync(company, korean)

    for p in patents:
        print(f"\n{p['Patent Number']}: {p['Title']}")
        print(f"  Targets: {p['Targets']}")
        print(f"  Area: {p['Therapeutic Area']}")
