"""
Configuration settings for FrontierAtlas Intelligence Pipeline.
Enforces type safety via pydantic-settings, reading environment variables from .env.
"""

import json
from typing import List, Optional
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded automatically from .env (case-insensitive)."""
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )

    # API Keys & LLM Provider Settings
    gemini_api_key: Optional[str] = None
    gemini_model: str = "gemini-3.5-flash-lite"
    
    # Tier 2: Groq / Secondary OpenAI-Compatible
    groq_api_key: Optional[str] = None
    groq_base_url: str = "https://api.groq.com/openai/v1"
    groq_model: str = "llama-3.3-70b-versatile"

    # Tier 3: Third-Party OpenAI-Compatible Provider (e.g. DeepSeek V4 Flash / Custom Gateway)
    custom_llm_api_key: Optional[str] = None
    custom_llm_base_url: Optional[str] = None
    custom_llm_model: Optional[str] = None
    deepseek_api_key: Optional[str] = None
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-chat"

    # GitHub API Token (for live repository star metrics)
    # GITHUB_TOKENS (comma-separated) enables key-pooled star enrichment at scale;
    # GITHUB_TOKEN remains the single-key fallback.
    github_token: Optional[str] = None
    github_tokens: Optional[str] = None

    @property
    def github_token_list(self) -> List[str]:
        """Resolve GitHub tokens: GITHUB_TOKENS (comma-separated) or the single GITHUB_TOKEN."""
        if self.github_tokens:
            return [t.strip() for t in self.github_tokens.split(",") if t.strip()]
        return [self.github_token] if self.github_token else []

    # LLM Operational Concurrency & Provider Rate Limits
    max_concurrent_llm_requests: int = 5
    gemini_rpm: int = 15
    groq_rpm: int = 30
    custom_llm_rpm: int = 60
    llm_call_timeout_seconds: float = 10.0
    llm_extract_timeout_seconds: float = 12.0

    # LLM key pools: comma-separated lists; falls back to the single-key fields above.
    gemini_api_keys: Optional[str] = None
    groq_api_keys: Optional[str] = None
    custom_llm_api_keys: Optional[str] = None
    deepseek_api_keys: Optional[str] = None

    @property
    def gemini_api_key_list(self) -> List[str]:
        """Resolve Gemini keys (pool or single)."""
        if self.gemini_api_keys:
            return [k.strip() for k in self.gemini_api_keys.split(",") if k.strip()]
        return [self.gemini_api_key] if self.gemini_api_key else []

    @property
    def groq_api_key_list(self) -> List[str]:
        """Resolve Groq keys (pool or single)."""
        if self.groq_api_keys:
            return [k.strip() for k in self.groq_api_keys.split(",") if k.strip()]
        return [self.groq_api_key] if self.groq_api_key else []

    @property
    def tier3_api_key_list(self) -> List[str]:
        """Resolve Tier 3 keys: custom gateway pool first, else DeepSeek pool, else singles."""
        if self.custom_llm_api_keys:
            return [k.strip() for k in self.custom_llm_api_keys.split(",") if k.strip()]
        if self.deepseek_api_keys:
            return [k.strip() for k in self.deepseek_api_keys.split(",") if k.strip()]
        return [self.effective_tier3_api_key] if self.effective_tier3_api_key else []

    @property
    def effective_tier3_api_key(self) -> Optional[str]:
        """Resolves Tier 3 API key (custom third-party key takes precedence)."""
        return self.custom_llm_api_key or self.deepseek_api_key

    @property
    def effective_tier3_base_url(self) -> str:
        """Resolves Tier 3 base URL (custom third-party base URL takes precedence)."""
        return self.custom_llm_base_url or self.deepseek_base_url

    @property
    def effective_tier3_model(self) -> str:
        """Resolves Tier 3 model name (custom third-party model takes precedence)."""
        return self.custom_llm_model or self.deepseek_model

    # Pipeline Performance & Concurrency
    max_concurrent_requests: int = 15
    default_request_timeout_seconds: float = 30.0
    token_budget_per_prompt: int = 3500

    # Crawler tuning (externalized for scale; defaults match the original hardcoded values)
    arxiv_interval_seconds: float = 3.2
    github_interval_seconds: float = 2.1
    github_anonymous_lookup_budget: int = 50
    papers_batch_size: int = 500
    openalex_per_page: int = 100
    hf_daily_limit: int = 100
    arxiv_search_query: str = (
        "(cat:cs.AI OR cat:cs.LG OR cat:cs.CV OR cat:cs.CL) AND (all:github OR all:code)"
    )
    arxiv_cdn_categories: str = "cs.AI,cs.LG,cs.CV,cs.CL,cs.NE"
    openalex_email: Optional[str] = None
    products_concurrency: int = 15
    yc_start_page: int = 2
    yc_max_page: int = 24
    yc_parallel_pages: int = 4
    github_search_pages: int = 10
    hf_models_limit: int = 1000
    wal_dir: str = "exports/wal"

    # News / job source knobs
    news_summary_preview_len: int = 300
    news_summary_max_len: int = 500
    jobs_remoteok_url: str = "https://remoteok.com/api?tag=ai"
    jobs_arbeitnow_url: str = "https://www.arbeitnow.com/api/job-board-api"
    jobs_himalayas_api_url: str = "https://himalayas.app/jobs/api?q=ai"
    jobs_himalayas_rss_url: str = "https://himalayas.app/jobs/rss"
    jobs_weworkremotely_urls: str = (
        "https://weworkremotely.com/remote-jobs.rss|"
        "https://weworkremotely.com/categories/remote-programming-jobs.rss|"
        "https://weworkremotely.com/categories/remote-customer-support-jobs.rss"
    )
    jobs_hnrss_url: str = "https://hnrss.org/whoishiring/jobs?q=AI"

    # Structured source overrides (JSON) — None = use the built-in defaults
    product_sources_json: Optional[str] = None
    news_sources_json: Optional[str] = None

    @classmethod
    def _parse_json_list(cls, raw: Optional[str], default: List[object]) -> List[object]:
        """Parse a JSON source-override string, falling back to the default list."""
        if not raw:
            return default
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, list) and parsed else default
        except (json.JSONDecodeError, TypeError):
            return default

    # Telemetry & Output
    log_level: str = "INFO"
    enable_wal: bool = False
    run_state_path: str = "exports/run_state.json"
    run_report_path: str = "exports/run_report.json"

    # Google Sheets Deliverable Export (gspread)
    google_service_account_path: Optional[str] = None
    google_sheets_credentials_path: Optional[str] = None
    google_sheets_spreadsheet_id: Optional[str] = None
    evaluator_email: Optional[str] = None
    sheets_batch_size: int = 500

    @property
    def effective_service_account_path(self) -> Optional[str]:
        """Resolves active service account path, prioritizing GOOGLE_SERVICE_ACCOUNT_PATH."""
        return self.google_service_account_path or self.google_sheets_credentials_path


# Global singleton instance
settings = Settings()
