"""
Content pre-processing, HTML stripping, and token budgeting (HTTP 413 defense).
Enforces Phase III specifications from PROJECT_CONTEXT.md.
"""

from typing import Optional
import tiktoken
import trafilatura
from bs4 import BeautifulSoup

from src.config import settings
from src.utils.logger import logger

_ENCODER = tiktoken.get_encoding("cl100k_base")


def clean_html_text(raw_html: str) -> str:
    """Extract clean readable text from HTML, discarding boilerplate and scripts."""
    if not raw_html:
        return ""
    extracted = trafilatura.extract(raw_html, include_links=True, include_tables=True)
    if extracted and len(extracted.strip()) > 30:
        return extracted.strip()

    soup = BeautifulSoup(raw_html, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header", "noscript", "svg", "form"]):
        tag.decompose()
    return soup.get_text(separator="\n", strip=True)


def chunk_to_budget(text: str, max_tokens: Optional[int] = None) -> str:
    """Semantically truncate text to stay strictly within token budget (HTTP 413 defense)."""
    budget = max_tokens or settings.token_budget_per_prompt
    if not text:
        return ""
    tokens = _ENCODER.encode(text)
    if len(tokens) <= budget:
        return text
    logger.debug(f"Truncating text from {len(tokens)} to budget {budget} tokens.")
    return _ENCODER.decode(tokens[:budget])
