"""
Structured logging system with automatic PII and secret redaction.
Enforces CODING_STANDARDS.md Pillar 4.7 and GUARDRAILS.md Module 6.4.
"""

import os
import re
import sys
from typing import Any, Dict
from loguru import logger

# Sensitive field keywords and credential pattern to mask automatically
SENSITIVE_KEY_RE = re.compile(
    r"(?i)(password|token|secret|api_?key|authorization|jwt|creditcard|ssn)"
)
CREDENTIAL_INLINE_RE = re.compile(
    r"(?i)(bearer\s+[a-zA-Z0-9_\-\.]{15,}|(api_?key|token|secret)\s*[:=]\s*['\"]?[a-zA-Z0-9_\-\.]{8,}['\"]?)"
)

LOG_FILE_PATH = os.path.join("logs", "pipeline.log")


def _mask_value(key: str, val: Any) -> Any:
    if isinstance(key, str) and SENSITIVE_KEY_RE.search(key):
        return "[REDACTED]"
    if isinstance(val, dict):
        return {k: _mask_value(k, v) for k, v in val.items()}
    if isinstance(val, list):
        return [_mask_value(key, item) for item in val]
    return val


def redact_record(record: Dict[str, Any]) -> bool:
    """Filter to mask credentials from extra context and message bodies."""
    if "extra" in record:
        for k, v in list(record["extra"].items()):
            record["extra"][k] = _mask_value(k, v)

    if isinstance(record.get("message"), str):
        record["message"] = CREDENTIAL_INLINE_RE.sub("[REDACTED_CREDENTIAL]", record["message"])
    return True


def setup_logging(log_level: str = "INFO") -> None:
    """Configure Loguru with stderr console + persistent file sink (telemetry source)."""
    logger.remove()
    log_format = (
        "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
        "<level>{level: <8}</level> | "
        "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
        "<level>{message}</level> "
        "{extra}"
    )
    logger.add(
        sys.stderr,
        format=log_format,
        level=log_level,
        filter=redact_record,
        backtrace=True,
        diagnose=False
    )
    # Persistent plain-text sink: escalation telemetry and run audits read from here.
    logger.add(
        LOG_FILE_PATH,
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} - {message} {extra}",
        level="DEBUG",
        filter=redact_record,
        rotation="10 MB",
        retention=5,
        enqueue=True,
        backtrace=False,
        diagnose=False,
    )


# Configure default logger on load
setup_logging()
__all__ = ["logger", "setup_logging"]
