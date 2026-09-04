"""Rate limiting definitions and retry utilities for LLM pipelines."""

from src.llm.fallback_chain import _LLM_RETRY


def create_llm_retry_decorator(max_attempts: int = 3):
    """Compatibility helper returning LLM retry decorator."""
    return _LLM_RETRY
