"""Clinical trials enrichment - ClinicalTrials.gov + publication search."""
import asyncio
import subprocess
import re
import json
import urllib.parse
from dataclasses import dataclass
from typing import Optional
from openai import AsyncOpenAI
from config import config
from utils.serper import search_serper
from utils.fetch import fetch_content

client = AsyncOpenAI(api_key=config.openai_api_key)

CT_API_BASE = "https://clinicaltrials.gov/api/v2/studies"


async def fetch_ct_api(params: dict) -> dict:
    """Fetch from ClinicalTrials.gov API using curl (avoids 403 issues)."""
    query_string = urllib.parse.urlencode(params)
    url = f"{CT_API_BASE}?{query_string}"

    proc = await asyncio.create_subprocess_exec(
        "curl", "-s", url,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()

    if proc.returncode != 0:
        return {"studies": []}

    try:
        return json.loads(stdout.decode())
    except:
        return {"studies": []}


@dataclass
class ClinicalTrial:
    """Clinical trial from ClinicalTrials.gov."""
    nct_id: str
    title: str
    phase: str
    status: str  # RECRUITING, COMPLETED, TERMINATED, etc.
    enrollment: int
    conditions: list[str]
    start_date: str
    completion_date: str
    drug_names: list[str]

    @property
    def phase_numeric(self) -> float:
        """Convert phase to numeric for comparison."""
        phase_map = {
            "EARLY_PHASE1": 0.5,
            "PHASE1": 1,
            "PHASE1/PHASE2": 1.5,
            "PHASE2": 2,
            "PHASE2/PHASE3": 2.5,
            "PHASE3": 3,
            "PHASE4": 4,
        }
        return phase_map.get(self.phase.upper().replace(" ", ""), 0)

    @property
    def phase_display(self) -> str:
        """Human-readable phase."""
        phase_map = {
            "EARLY_PHASE1": "Phase 1 (Early)",
            "PHASE1": "Phase 1",
            "PHASE1/PHASE2": "Phase 1/2",
            "PHASE2": "Phase 2",
            "PHASE2/PHASE3": "Phase 2/3",
            "PHASE3": "Phase 3",
            "PHASE4": "Phase 4 (Post-market)",
            "NA": "N/A",
        }
        return phase_map.get(self.phase.upper().replace(" ", ""), self.phase)


@dataclass
class TrialPublication:
    """Publication related to a clinical trial."""
    title: str
    url: str
    source: str  # "ASCO", "PubMed", "AACR", etc.
    year: str
    summary: str


@dataclass
class ClinicalEnrichment:
    """Enrichment data for a pipeline asset."""
    verified_phase: str  # Phase from ClinicalTrials.gov
    trial_status: str  # RECRUITING, COMPLETED, etc.
    nct_id: str
    enrollment: int
    publications: list[TrialPublication]
    data_summary: str  # Key efficacy/safety data if available


async def search_clinical_trials(
    drug_name: str,
    company: str,
    max_results: int = 10,
) -> list[ClinicalTrial]:
    """
    Search ClinicalTrials.gov for trials matching drug and company.

    Tries multiple search strategies to find relevant trials.
    """
    trials = []
    seen_ncts = set()

    # Clean drug name (remove company suffix if present)
    drug_clean = re.sub(r'\s*\(.*\)', '', drug_name).strip()

    # Search strategies - search by sponsor first (most reliable), then drug name
    queries = [
        ("spons", company),  # Search by sponsor
        ("term", drug_clean),  # Search by drug name
    ]

    for query_type, query_value in queries:
        try:
            params = {
                f"query.{query_type}": query_value,
                "fields": "NCTId,BriefTitle,OverallStatus,Phase,StartDate,PrimaryCompletionDate,EnrollmentCount,Condition,InterventionName,LeadSponsorName",
                "pageSize": str(max_results),
            }
            data = await fetch_ct_api(params)

            for study in data.get("studies", []):
                proto = study.get("protocolSection", {})
                ident = proto.get("identificationModule", {})
                status_mod = proto.get("statusModule", {})
                design = proto.get("designModule", {})
                arms = proto.get("armsInterventionsModule", {})
                cond = proto.get("conditionsModule", {})
                sponsor = proto.get("sponsorCollaboratorsModule", {})

                nct_id = ident.get("nctId", "")
                if nct_id in seen_ncts:
                    continue
                seen_ncts.add(nct_id)

                # Extract intervention/drug names
                interventions = arms.get("interventions", [])
                drug_names = [i.get("name", "") for i in interventions if i.get("type") == "DRUG"]

                # Check if this trial is relevant
                title = ident.get("briefTitle", "")
                lead_sponsor = sponsor.get("leadSponsor", {}).get("name", "")

                drug_in_title = drug_clean.lower() in title.lower()
                drug_in_interventions = drug_clean.lower() in " ".join(drug_names).lower()
                company_matches = company.lower() in lead_sponsor.lower()

                # Require BOTH drug match AND company match to avoid false positives
                # (e.g., different companies using same drug code like ABL001)
                if query_type == "spons":
                    # Sponsor search - already filtered by company, just check drug
                    is_relevant = drug_in_title or drug_in_interventions
                else:
                    # Term search - require drug match AND company match
                    is_relevant = (drug_in_title or drug_in_interventions) and company_matches

                if not is_relevant:
                    continue

                trials.append(ClinicalTrial(
                    nct_id=nct_id,
                    title=title,
                    phase=design.get("phases", ["NA"])[0] if design.get("phases") else "NA",
                    status=status_mod.get("overallStatus", "UNKNOWN"),
                    enrollment=status_mod.get("enrollmentInfo", {}).get("count", 0),
                    conditions=cond.get("conditions", []),
                    start_date=status_mod.get("startDateStruct", {}).get("date", ""),
                    completion_date=status_mod.get("primaryCompletionDateStruct", {}).get("date", ""),
                    drug_names=drug_names,
                ))

        except Exception as e:
            continue

    return trials


def get_most_advanced_phase(trials: list[ClinicalTrial]) -> tuple[str, ClinicalTrial]:
    """
    Determine the most advanced phase from a list of trials.

    Returns: (phase_display, most_advanced_trial)
    """
    if not trials:
        return "", None

    # Sort by phase (descending) then by status (COMPLETED > RECRUITING > others)
    status_priority = {"COMPLETED": 0, "ACTIVE_NOT_RECRUITING": 1, "RECRUITING": 2}

    sorted_trials = sorted(
        trials,
        key=lambda t: (-t.phase_numeric, status_priority.get(t.status, 99))
    )

    best = sorted_trials[0]
    return best.phase_display, best


async def find_trial_publications(
    drug_name: str,
    company: str,
    nct_id: str = None,
) -> list[TrialPublication]:
    """
    Search for publications related to a drug's clinical trials.

    Searches ASCO, PubMed, AACR abstracts.
    """
    publications = []
    drug_clean = re.sub(r'\s*\(.*\)', '', drug_name).strip()

    # Build search query - include company name to avoid drug code collisions
    # (e.g., ABL001 is both ABL Bio's VEGF/DLL4 and Novartis's asciminib)
    query = f'"{drug_clean}" "{company}" clinical trial results efficacy'
    if nct_id:
        query = f'{nct_id} OR ({query})'

    try:
        results = await search_serper(query, num_results=8)

        # Filter for relevant publication sources
        pub_domains = ["asco.org", "ascopubs.org", "pubmed", "pmc.ncbi", "aacrjournals.org", "nejm.org", "thelancet.com", "nature.com"]

        for r in results:
            is_pub = any(domain in r.link.lower() for domain in pub_domains)
            if not is_pub:
                continue

            # Determine source
            if "asco" in r.link.lower():
                source = "ASCO"
            elif "pubmed" in r.link.lower() or "pmc.ncbi" in r.link.lower():
                source = "PubMed"
            elif "aacr" in r.link.lower():
                source = "AACR"
            elif "nejm" in r.link.lower():
                source = "NEJM"
            elif "lancet" in r.link.lower():
                source = "Lancet"
            else:
                source = "Journal"

            # Extract year from snippet or title
            year_match = re.search(r'20[12]\d', r.snippet + r.title)
            year = year_match.group() if year_match else ""

            publications.append(TrialPublication(
                title=r.title,
                url=r.link,
                source=source,
                year=year,
                summary=r.snippet[:200],
            ))
    except Exception as e:
        pass

    return publications


async def fetch_publication_content(url: str) -> str:
    """
    Fetch full content from a publication URL.

    Handles ASCO, PubMed, AACR pages.
    """
    try:
        result = await fetch_content(url, use_cache=True)
        if result.method != "failed" and len(result.text) > 200:
            return result.text[:15000]  # Limit to avoid token issues
    except Exception as e:
        pass
    return ""


async def extract_efficacy_data(
    publications: list[TrialPublication],
    drug_name: str,
) -> str:
    """
    Fetch full publication content and extract efficacy/safety data.

    1. Fetch full page content for top 2 publications
    2. Use LLM to extract detailed efficacy data
    """
    if not publications:
        return ""

    # Fetch full content for top publications (prioritize ASCO, then PubMed)
    priority_order = ["ASCO", "AACR", "PubMed", "NEJM", "Lancet", "Journal"]
    sorted_pubs = sorted(
        publications[:5],
        key=lambda p: priority_order.index(p.source) if p.source in priority_order else 99
    )

    # Fetch full content for top 2 publications
    full_contents = []
    for pub in sorted_pubs[:2]:
        content = await fetch_publication_content(pub.url)
        if content and len(content) > 500:
            full_contents.append(f"[{pub.source} {pub.year}] {pub.title}\nURL: {pub.url}\n\n{content}")
        else:
            # Fallback to snippet if fetch fails
            full_contents.append(f"[{pub.source} {pub.year}] {pub.title}\n{pub.summary}")

    if not full_contents:
        return ""

    combined_content = "\n\n---\n\n".join(full_contents)

    prompt = f"""Extract the key clinical trial efficacy and safety data for {drug_name} from these publication contents.

{combined_content}

Extract and return a concise summary (2-3 sentences max) with SPECIFIC NUMBERS:
- Overall Response Rate (ORR) and/or Disease Control Rate (DCR)
- Number of patients treated (N=)
- Key safety findings (DLTs, common AEs, any deaths)
- Median PFS/OS if available

Format example: "Phase 1b (N=39): ORR 15.4%, DCR 69.2%; Grade 3+ AEs in 23% (fatigue, elevated AST); no DLTs at RP2D."

If the content doesn't contain efficacy data, return "No efficacy data in publications."
"""

    try:
        response = await client.chat.completions.create(
            model=config.text_model,
            messages=[
                {"role": "system", "content": "Extract clinical trial efficacy data with specific numbers. Be precise and concise."},
                {"role": "user", "content": prompt}
            ],
            max_tokens=200,
        )
        return response.choices[0].message.content.strip()
    except:
        return ""


async def enrich_asset_with_trials(
    asset: dict,
    company: str,
) -> ClinicalEnrichment:
    """
    Full clinical trial enrichment for a pipeline asset.

    1. Search ClinicalTrials.gov for trials
    2. Determine most advanced phase
    3. Find publications if Phase 1+
    4. Extract efficacy data
    """
    drug_name = asset.get("Asset Name", "")

    if not drug_name or drug_name == "Undisclosed":
        return ClinicalEnrichment(
            verified_phase="",
            trial_status="",
            nct_id="",
            enrollment=0,
            publications=[],
            data_summary="",
        )

    # Search trials
    trials = await search_clinical_trials(drug_name, company)

    if not trials:
        return ClinicalEnrichment(
            verified_phase="",
            trial_status="",
            nct_id="",
            enrollment=0,
            publications=[],
            data_summary="",
        )

    # Get most advanced phase
    verified_phase, best_trial = get_most_advanced_phase(trials)

    # Find publications if Phase 1+ and likely to have data
    # (COMPLETED trials definitely have data, RECRUITING may have interim)
    publications = []
    data_summary = ""

    should_search_data = (
        best_trial and
        best_trial.phase_numeric >= 1 and
        best_trial.status in ["COMPLETED", "ACTIVE_NOT_RECRUITING", "RECRUITING", "TERMINATED"]
    )

    if should_search_data:
        publications = await find_trial_publications(
            drug_name, company, best_trial.nct_id
        )

        if publications:
            data_summary = await extract_efficacy_data(publications, drug_name)

    return ClinicalEnrichment(
        verified_phase=verified_phase,
        trial_status=best_trial.status if best_trial else "",
        nct_id=best_trial.nct_id if best_trial else "",
        enrollment=best_trial.enrollment if best_trial else 0,
        publications=publications,
        data_summary=data_summary,
    )


async def enrich_assets_with_trials(
    assets: list[dict],
    company: str,
    max_concurrent: int = 3,
) -> list[tuple[dict, ClinicalEnrichment]]:
    """
    Enrich multiple assets with clinical trial data.
    """
    semaphore = asyncio.Semaphore(max_concurrent)

    async def enrich_one(asset):
        async with semaphore:
            enrichment = await enrich_asset_with_trials(asset, company)
            return asset, enrichment

    tasks = [enrich_one(asset) for asset in assets]
    return await asyncio.gather(*tasks)


def apply_trial_enrichment(
    asset: dict,
    enrichment: ClinicalEnrichment,
) -> dict:
    """
    Apply clinical trial enrichment to asset, updating Phase if better data.
    """
    updated = asset.copy()

    # Update Phase if ClinicalTrials.gov has data
    if enrichment.verified_phase:
        updated["Phase"] = enrichment.verified_phase
        updated["Phase Source"] = "ClinicalTrials.gov"

    # Add trial metadata
    if enrichment.nct_id:
        updated["NCT ID"] = enrichment.nct_id
        updated["Trial Status"] = enrichment.trial_status
        updated["Enrollment"] = enrichment.enrollment

    # Add efficacy data if available
    if enrichment.data_summary:
        updated["Clinical Data"] = enrichment.data_summary

    # Add publication links
    if enrichment.publications:
        pub_links = "; ".join([p.url for p in enrichment.publications[:2]])
        updated["Publications"] = pub_links

    return updated


# Sync wrapper for testing
def enrich_with_trials_sync(drug_name: str, company: str) -> dict:
    """Test a single drug."""
    async def run():
        asset = {"Asset Name": drug_name}
        enrichment = await enrich_asset_with_trials(asset, company)
        return {
            "verified_phase": enrichment.verified_phase,
            "trial_status": enrichment.trial_status,
            "nct_id": enrichment.nct_id,
            "enrollment": enrichment.enrollment,
            "data_summary": enrichment.data_summary,
            "publications": [p.url for p in enrichment.publications],
        }
    return asyncio.run(run())


if __name__ == "__main__":
    import sys
    drug = sys.argv[1] if len(sys.argv) > 1 else "ABL001"
    company = sys.argv[2] if len(sys.argv) > 2 else "ABL Bio"

    print(f"Enriching: {drug} ({company})")
    result = enrich_with_trials_sync(drug, company)

    print(f"\nPhase: {result['verified_phase']}")
    print(f"Status: {result['trial_status']}")
    print(f"NCT: {result['nct_id']}")
    print(f"Enrollment: {result['enrollment']}")
    print(f"\nData: {result['data_summary']}")
    print(f"\nPublications:")
    for url in result['publications']:
        print(f"  - {url}")
