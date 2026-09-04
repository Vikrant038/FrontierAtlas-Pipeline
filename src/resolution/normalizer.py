import json
import os
import re
import unicodedata
from urllib.parse import urlparse
from typing import Dict, List, Optional, Set, Tuple
from rapidfuzz import fuzz, process

from src.schemas.entities import EntityResolutionLog, MatchMethodEnum
from src.utils.logger import logger
from src.resolution.seed_data import (
    CANONICAL_AI_ENTITIES,
    CORPORATE_SUFFIXES,
    TECH_NOISE_WORDS,
    KNOWN_ALIASES,
)


def extract_domain(url: str) -> str:
    """Extract root domain from URL (e.g. 'https://api.openai.com/v1' -> 'openai.com')."""
    if not url or not isinstance(url, str):
        return ""
    host = (urlparse(url).hostname or "").lower()
    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def normalize_string_tier1(name: str) -> str:
    """Normalize string: unicode NFKD, lowercase, remove punctuation, strip noise words & suffixes."""
    if not name:
        return ""
    name = "".join(c for c in unicodedata.normalize("NFKD", name.strip().lower()) if not unicodedata.combining(c))
    tokens = [t for t in re.sub(r"[^\w\s]", " ", name).split() if t not in CORPORATE_SUFFIXES and t not in TECH_NOISE_WORDS]
    return " ".join(tokens)


PUBLIC_AGGREGATORS = {
    "example.com", "github.com", "techcrunch.com", "venturebeat.com",
    "theverge.com", "arxiv.org", "huggingface.co", "remoteok.com",
    "arbeitnow.com", "ycombinator.com", "himalayas.app"
}


def _is_official_domain(domain: str, normalized_name: str) -> bool:
    """Validate that domain actually belongs to entity and is not a third-party aggregator."""
    if not domain or domain in PUBLIC_AGGREGATORS or not normalized_name:
        return False
    domain_stem = domain.split(".")[0]
    return domain_stem in normalized_name or normalized_name in domain_stem


class EntityResolver:
    """3-Tier Entity Resolution Engine with dynamic entity learning, domain grounding, and disk caching."""

    def __init__(self, seed_entities: Optional[List[str]] = None, cache_path: str = "exports/canonical_registry.json"):
        self.cache_path = cache_path
        self.canonical_entities: Set[str] = set(seed_entities or CANONICAL_AI_ENTITIES)
        self.normalized_map: Dict[str, str] = {normalize_string_tier1(e): e for e in self.canonical_entities}
        self.domain_map: Dict[str, str] = {}
        self.audit_log: List[EntityResolutionLog] = []
        self._load_cache()

    def _load_cache(self) -> None:
        """Load previously learned entities and domain mappings from disk if cache exists."""
        if not os.path.exists(self.cache_path):
            return
        try:
            with open(self.cache_path, "r", encoding="utf-8") as cache_file:
                cached_registry = json.load(cache_file)
        except (json.JSONDecodeError, OSError) as cache_exc:
            # Corrupt or unreadable cache: fall back to base seed state, never crash startup.
            logger.warning(f"Canonical registry cache unreadable ({cache_exc}); using seed entities only.")
            return
        for entity in cached_registry.get("entities", []):
            self.canonical_entities.add(entity)
            self.normalized_map[normalize_string_tier1(entity)] = entity
        self.domain_map.update(cached_registry.get("domains", {}))

    def save_cache(self) -> None:
        """Persist dynamically learned entities and domain grounding registry to disk."""
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self.cache_path)), exist_ok=True)
            with open(self.cache_path, "w", encoding="utf-8") as cache_file:
                json.dump({
                    "entities": sorted(list(self.canonical_entities)),
                    "domains": self.domain_map
                }, cache_file, indent=2)
            logger.info(
                f"Canonical registry persisted: {len(self.canonical_entities)} entities, "
                f"{len(self.domain_map)} grounded domains -> {self.cache_path}"
            )
        except OSError as cache_exc:
            logger.error(f"Failed to persist canonical registry to {self.cache_path}: {cache_exc}")

    def resolve(
        self,
        raw_name: str,
        source_url: str = "",
        entity_type: str = "STARTUP",
    ) -> Tuple[str, EntityResolutionLog]:
        """Resolve raw name through 4 tiers: Domain Grounding -> Aliases -> Normalization -> Fuzzy / Register."""
        cleaned = (raw_name or "").strip()
        lower_cleaned = cleaned.lower()
        normalized = normalize_string_tier1(cleaned)
        domain = extract_domain(source_url)

        if not cleaned:
            canonical, method, conf = "Unknown", MatchMethodEnum.MANUAL_OVERRIDE, 0.0
        # Tier 0: Domain URL Grounding (100% precision anchor when domain is known)
        elif domain and domain in self.domain_map:
            canonical, method, conf = self.domain_map[domain], MatchMethodEnum.NORMALIZATION_EXACT, 1.00
        # Tier 1A: Known Exact Aliases
        elif lower_cleaned in KNOWN_ALIASES:
            canonical, method, conf = KNOWN_ALIASES[lower_cleaned], MatchMethodEnum.ALIAS_MATCH, 1.00
        # Tier 1B: NFKD String Normalization
        elif normalized in self.normalized_map:
            canonical, method, conf = self.normalized_map[normalized], MatchMethodEnum.NORMALIZATION_EXACT, 1.00
        else:
            # Tier 2: C-accelerated Fuzzy Token Sort Matching
            best_match = process.extractOne(
                cleaned,
                self.canonical_entities,
                scorer=fuzz.token_sort_ratio,
                score_cutoff=90.0,
            )
            if best_match:
                canonical, method, conf = best_match[0], MatchMethodEnum.FUZZY_TOKEN_SORT, round(best_match[1] / 100.0, 2)
            else:
                # Dynamic Learning: Register new entity
                canonical, method, conf = cleaned, MatchMethodEnum.NEW_ENTITY, 0.50
                self.canonical_entities.add(canonical)
                if normalized:
                    self.normalized_map[normalized] = canonical

        # Bind official domain if valid (applies to all non-Tier-0 matches)
        if _is_official_domain(domain, normalized):
            self.domain_map[domain] = canonical

        log = EntityResolutionLog(
            rawName=cleaned or "Unknown",
            canonicalName=canonical,
            entityType=entity_type,
            matchMethod=method,
            confidenceScore=conf,
            sourceUrl=source_url,
        )
        self.audit_log.append(log)
        return canonical, log


# Global resolver instance
entity_resolver = EntityResolver()
