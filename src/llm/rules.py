"""
Shared deterministic classification rules for pricing models and job role families.
Single source of truth consumed by both the crawler keyword tiers (Tier 4 fallback)
and the LLM engine's deterministic extractor, guaranteeing the two never drift.
"""

import re
from typing import List, Tuple

from src.schemas.entities import PricingModelEnum, RoleFamilyEnum

# Ordered keyword tiers: first match wins (most specific signal first).
PRICING_KEYWORD_TIERS: List[Tuple[PricingModelEnum, Tuple[str, ...]]] = [
    (PricingModelEnum.ENTERPRISE, ("ENTERPRISE", "CONTACT SALES", "REQUEST DEMO", "CUSTOM PRICING")),
    (PricingModelEnum.PAID, ("$", "/MO", "/MONTH", "SUBSCRIPTION", "PER TOKEN", "PAY-AS-YOU-GO", "PAY AS YOU GO")),
    (PricingModelEnum.FREE, ("OPEN SOURCE", "OPEN-SOURCE", "100% FREE", "FREE TOOL", "COMPLETELY FREE", "MIT LICENSE", "APACHE 2")),
    (PricingModelEnum.FREEMIUM, ("FREEMIUM", "FREE TIER", "FREE TRIAL", "FREE PLAN", "FREE VERSION")),
    (PricingModelEnum.PAID, ("API", "INFRASTRUCTURE", "HOSTED PLATFORM", "CLOUD SERVICE")),
]

PRICING_PLATFORM_FREE_DOMAINS = ("github.com", "huggingface.co")

REMOTE_SIGNALS = ("remote", "worldwide", "anywhere", "work from home", "telecommute")

ONSITE_SIGNALS = ("onsite", "on-site", "in-person")

# Ordered role patterns: first match wins.
ROLE_MAP: List[Tuple[RoleFamilyEnum, re.Pattern]] = [
    (RoleFamilyEnum.RESEARCH, re.compile(r"research|scientist|phd|postdoc", re.I)),
    (RoleFamilyEnum.ENGINEERING, re.compile(r"engineer|developer|architect|programmer|mlops|backend", re.I)),
    (RoleFamilyEnum.PRODUCT, re.compile(r"product|pm\b", re.I)),
    (RoleFamilyEnum.DESIGN, re.compile(r"design|ui|ux", re.I)),
    (RoleFamilyEnum.SALES, re.compile(r"sales|bdr|sdr|account exec", re.I)),
    (RoleFamilyEnum.MARKETING, re.compile(r"marketing|growth", re.I)),
    (RoleFamilyEnum.OPERATIONS, re.compile(r"operations|ops|chief of staff", re.I)),
]


def classify_pricing_by_keywords(name: str, url: str, desc: str) -> PricingModelEnum:
    """Classify pricing model from keyword tiers, platform domains, then default."""
    text = f"{name} {desc}".upper()
    url_lower = (url or "").lower()
    for pricing_model, keywords in PRICING_KEYWORD_TIERS:
        if any(w in text for w in keywords):
            return pricing_model
    if any(d in url_lower for d in PRICING_PLATFORM_FREE_DOMAINS):
        return PricingModelEnum.FREE
    return PricingModelEnum.FREEMIUM


def classify_role_family(text: str) -> RoleFamilyEnum:
    """Classify role family from ordered regex patterns; OTHER when nothing matches."""
    for role_family, pattern in ROLE_MAP:
        if pattern.search(text or ""):
            return role_family
    return RoleFamilyEnum.OTHER
