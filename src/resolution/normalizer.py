import asyncio
import concurrent.futures
import json
import os
import re
import unicodedata
from urllib.parse import urlparse
from typing import Dict, List, Optional, Set, Tuple
from rapidfuzz import fuzz, process

from src.schemas.entities import EntityResolutionLog, MatchMethodEnum
from src.llm.fallback_chain import llm_engine
from src.llm.prompts import EntityDisambiguationSchema, ENTITY_DISAMBIGUATION_PROMPT
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
    """3-Tier Entity Resolution Engine with dynamic entity learning, domain grounding, and LLM disambiguation."""

    def __init__(
        self,
        seed_entities: Optional[List[str]] = None,
        cache_path: str = "exports/canonical_registry.json",
        enable_llm_disambiguation: bool = True,
        max_llm_disambiguations: int = 100,
    ):
        self.cache_path = cache_path
        self.canonical_entities: Set[str] = set(seed_entities or CANONICAL_AI_ENTITIES)
        self.normalized_map: Dict[str, str] = {normalize_string_tier1(e): e for e in self.canonical_entities}
        self.domain_map: Dict[str, str] = {}
        self.audit_log: List[EntityResolutionLog] = []
        self.enable_llm_disambiguation: bool = enable_llm_disambiguation
        self.max_llm_disambiguations: int = max_llm_disambiguations
        self._disambiguation_count: int = 0
        self._disambiguation_cache: Dict[str, Tuple[str, MatchMethodEnum, float]] = {}
        self._load_cache()

    def _load_cache(self) -> None:
        """Load previously learned entities and domain mappings from disk if cache exists."""
        if not os.path.exists(self.cache_path):
            return
        try:
            with open(self.cache_path, "r", encoding="utf-8") as cache_file:
                cached_registry = json.load(cache_file)
        except (json.JSONDecodeError, OSError) as cache_exc:
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

    def _check_deterministic_tiers(
        self,
        cleaned: str,
        lower_cleaned: str,
        normalized: str,
        domain: str,
    ) -> Tuple[Optional[Tuple[str, MatchMethodEnum, float]], List[Tuple[str, float]]]:
        """Check Tiers 0, 1A, 1B, and 2. Returns (match_result, top_candidates)."""
        if not cleaned:
            return ("Unknown", MatchMethodEnum.MANUAL_OVERRIDE, 0.0), []

        if domain and domain in self.domain_map:
            return (self.domain_map[domain], MatchMethodEnum.NORMALIZATION_EXACT, 1.00), []

        if lower_cleaned in KNOWN_ALIASES:
            return (KNOWN_ALIASES[lower_cleaned], MatchMethodEnum.ALIAS_MATCH, 1.00), []

        if normalized in self.normalized_map:
            return (self.normalized_map[normalized], MatchMethodEnum.NORMALIZATION_EXACT, 1.00), []

        top_matches = process.extract(
            cleaned,
            self.canonical_entities,
            scorer=fuzz.token_sort_ratio,
            limit=3,
        )
        cand_tuples = [(m[0], m[1]) for m in top_matches]
        best_score = top_matches[0][1] if top_matches else 0.0

        if top_matches and best_score >= 90.0:
            return (top_matches[0][0], MatchMethodEnum.FUZZY_TOKEN_SORT, round(best_score / 100.0, 2)), cand_tuples

        cache_key = lower_cleaned
        if cache_key in self._disambiguation_cache:
            return self._disambiguation_cache[cache_key], cand_tuples

        if top_matches and 70.0 <= best_score < 90.0 and self.enable_llm_disambiguation:
            if self._disambiguation_count < self.max_llm_disambiguations:
                return None, cand_tuples
            logger.warning(f"Disambiguation budget reached ({self.max_llm_disambiguations}); falling back to NEW_ENTITY.")

        return (cleaned, MatchMethodEnum.NEW_ENTITY, 0.50), cand_tuples

    async def _disambiguate_with_llm(
        self,
        cleaned: str,
        top_matches: List[Tuple[str, float]],
        source_url: str = "",
    ) -> Tuple[str, MatchMethodEnum, float]:
        """Query LLM fallback chain to disambiguate raw entity against top 3 fuzzy candidates."""
        cand_names = [m[0] for m in top_matches]
        cand_lines = "\n".join(f"- {name} (Fuzzy score: {score:.1f})" for name, score in top_matches)
        domain = extract_domain(source_url)
        prompt_content = (
            f"Raw Entity: {cleaned}\n"
            f"Source Domain: {domain or 'N/A'}\n"
            f"Top Canonical Candidates:\n{cand_lines}"
        )
        try:
            res = await llm_engine.extract_structured(
                raw_text=prompt_content,
                schema_cls=EntityDisambiguationSchema,
                instruction=ENTITY_DISAMBIGUATION_PROMPT,
            )
            if res.canonical:
                matched = next((c for c in cand_names if c.lower() == res.canonical.strip().lower()), None)
                if matched:
                    conf = round(float(res.confidence), 2) if res.confidence > 0 else 0.85
                    logger.info(f"LLM disambiguated '{cleaned}' -> '{matched}' (conf: {conf})")
                    return matched, MatchMethodEnum.LLM_DISAMBIGUATION, conf
            logger.debug(f"LLM returned NONE/unknown for '{cleaned}'. Falling back to NEW_ENTITY.")
        except Exception as exc:
            logger.debug(f"LLM disambiguation error for '{cleaned}': {exc}. Falling back to NEW_ENTITY.")
        return cleaned, MatchMethodEnum.NEW_ENTITY, 0.50

    def _record_resolution(
        self,
        cleaned: str,
        canonical: str,
        method: MatchMethodEnum,
        conf: float,
        source_url: str,
        entity_type: str,
        normalized: str,
        domain: str,
    ) -> Tuple[str, EntityResolutionLog]:
        """Update entity registry, cache domain grounding, and append audit log."""
        if method == MatchMethodEnum.NEW_ENTITY:
            self.canonical_entities.add(canonical)
            if normalized:
                self.normalized_map[normalized] = canonical
        elif method == MatchMethodEnum.LLM_DISAMBIGUATION:
            self._disambiguation_cache[cleaned.lower()] = (canonical, method, conf)


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

    async def resolve_async(
        self,
        raw_name: str,
        source_url: str = "",
        entity_type: str = "STARTUP",
    ) -> Tuple[str, EntityResolutionLog]:
        """Asynchronous entity resolution entrypoint with Tier 3 LLM fallback."""
        cleaned = (raw_name or "").strip()
        lower_cleaned = cleaned.lower()
        normalized = normalize_string_tier1(cleaned)
        domain = extract_domain(source_url)

        det_result, top_matches = self._check_deterministic_tiers(cleaned, lower_cleaned, normalized, domain)
        if det_result is not None:
            canonical, method, conf = det_result
        else:
            self._disambiguation_count += 1
            canonical, method, conf = await self._disambiguate_with_llm(cleaned, top_matches, source_url)

        return self._record_resolution(
            cleaned, canonical, method, conf, source_url, entity_type, normalized, domain
        )

    def resolve(
        self,
        raw_name: str,
        source_url: str = "",
        entity_type: str = "STARTUP",
    ) -> Tuple[str, EntityResolutionLog]:
        """Synchronous entity resolution entrypoint with zero-overhead fast-path."""
        cleaned = (raw_name or "").strip()
        lower_cleaned = cleaned.lower()
        normalized = normalize_string_tier1(cleaned)
        domain = extract_domain(source_url)

        det_result, top_matches = self._check_deterministic_tiers(cleaned, lower_cleaned, normalized, domain)
        if det_result is not None:
            canonical, method, conf = det_result
            return self._record_resolution(
                cleaned, canonical, method, conf, source_url, entity_type, normalized, domain
            )

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                return executor.submit(
                    asyncio.run, self.resolve_async(raw_name, source_url, entity_type)
                ).result()
        else:
            return asyncio.run(self.resolve_async(raw_name, source_url, entity_type))


# Global resolver instance
entity_resolver = EntityResolver()

