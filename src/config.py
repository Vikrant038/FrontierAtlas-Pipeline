"""
Configuration settings for FrontierAtlas Intelligence Pipeline.
Enforces type safety via pydantic-settings, reading environment variables from .env.
"""

from typing import Optional
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
    github_token: Optional[str] = None

    # LLM Operational Concurrency & Provider Rate Limits
    max_concurrent_llm_requests: int = 5
    gemini_rpm: int = 15
    groq_rpm: int = 30
    custom_llm_rpm: int = 60

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
    arxiv_rate_limit: float = 2.5

    # Telemetry & Output
    log_level: str = "INFO"
    export_directory: str = "exports"

    # Google Sheets Deliverable Export (gspread)
    google_service_account_path: Optional[str] = None
    google_sheets_credentials_path: Optional[str] = None
    google_sheets_spreadsheet_id: Optional[str] = None
    evaluator_email: Optional[str] = None

    @property
    def effective_service_account_path(self) -> Optional[str]:
        """Resolves active service account path, prioritizing GOOGLE_SERVICE_ACCOUNT_PATH."""
        return self.google_service_account_path or self.google_sheets_credentials_path


# Global singleton instance
settings = Settings()
