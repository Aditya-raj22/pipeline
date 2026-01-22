"""Centralized configuration for pipeline sourcing system."""
import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()


@dataclass
class Config:
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
    serper_api_key: str = os.getenv("SERPER_API_KEY", "")

    # Concurrency
    max_concurrent_companies: int = 3
    max_concurrent_fetches: int = 5
    max_drug_pages_per_company: int = 5

    # Enrichment
    max_enrichment_sources: int = 3
    deep_fetch_threshold: float = 0.5

    # Cache
    cache_ttl: int = 86400  # 24 hours
    cache_dir: str = ".cache"

    # Fetch thresholds (from existing scrape_pipelines.py)
    text_threshold: int = 500
    vision_threshold: int = 300

    # Retry
    max_retries: int = 3
    retry_backoff: tuple = (1, 3, 10)

    # Models - gpt-4o-mini is cheapest with good structured output + vision
    text_model: str = "gpt-4o-mini"
    vision_model: str = "gpt-4o-mini"


config = Config()
