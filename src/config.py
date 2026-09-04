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

    # API Keys
    gemini_api_key: Optional[str] = None
    groq_api_key: Optional[str] = None
    deepseek_api_key: Optional[str] = None
    github_token: Optional[str] = None

    # Pipeline Performance & Concurrency
    max_concurrent_requests: int = 15
    default_request_timeout_seconds: float = 30.0
    token_budget_per_prompt: int = 3500
    arxiv_rate_limit: float = 2.5

    # Telemetry & Output
    log_level: str = "INFO"
    export_directory: str = "exports"


# Global singleton instance
settings = Settings()
