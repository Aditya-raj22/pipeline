"""Centralized configuration for pipeline sourcing system."""
import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()


@dataclass
class Config:
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "")

    # Concurrency
    max_concurrent_companies: int = 3
    max_concurrent_fetches: int = 5
    max_drug_pages_per_company: int = 50

    # Enrichment
    max_enrichment_sources: int = 3
    deep_fetch_threshold: float = 0.5

    # Cache
    cache_ttl: int = 86400  # 24 hours
    cache_dir: str = ".cache"

    # Fetch thresholds
    text_threshold: int = 500      # Min text to consider HTTP fetch usable
    vision_threshold: int = 300    # Below this, force vision-only
    hybrid_threshold: int = 3000   # Below this, also get screenshot for hybrid extraction

    # Retry
    max_retries: int = 3
    retry_backoff: tuple = (1, 3, 10)

    # Models - gpt-4.1-mini: best accuracy/$ for structured extraction + vision
    # 72.7% MMMU vision, 1M context, $0.40/1M in, $1.60/1M out
    text_model: str = "gpt-4.1-mini"
    vision_model: str = "gpt-4.1-mini"


config = Config()
