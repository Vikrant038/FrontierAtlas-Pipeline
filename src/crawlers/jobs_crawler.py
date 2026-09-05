import asyncio
import re
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, List, Optional, Tuple

from src.config import settings
from src.crawlers.base import AsyncBaseCrawler
from src.llm.fallback_chain import llm_engine
from src.llm.prompts import JOB_EXTRACTION_PROMPT, JobExtractionSchema
from src.llm.rules import ONSITE_SIGNALS, REMOTE_SIGNALS, classify_role_family
from src.schemas.entities import JobContent, JobRecord, RoleFamilyEnum, SourceMetadata
from src.resolution.normalizer import entity_resolver
from src.utils.date_normalizer import infer_content_freshness
from src.utils.logger import logger
from src.utils.run_state import load_seen_keys, save_seen_keys, save_source_freshness

AI_KEYWORD_PATTERN = re.compile(
    r"\bai\b|machine learning|\bml\b|\bllm\b|data scien|deep learning",
    re.IGNORECASE,
)


class JobsCrawler(AsyncBaseCrawler):
    """Crawler for ingesting fresh (<24h) AI job postings across 5 AI job boards."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        # Keys collected in the PREVIOUS run: cross-run novelty heuristic state.
        self._prev_run_urls = load_seen_keys("jobs")
        self._novelty_keys: set = set()
        self._live_count: int = 0  # fresh records built so far (live progress reporting)

    @staticmethod
    def _is_remote(location: str = "", tags: Optional[List[str]] = None, title: str = "") -> bool:
        loc_str = (location or "").strip().lower()
        title_str = (title or "").lower()
        tag_str = " ".join(tags or []).lower()
        if any(w in loc_str or w in tag_str or w in title_str for w in REMOTE_SIGNALS):
            return True
        return not loc_str and not any(w in title_str for w in ONSITE_SIGNALS)

    async def _build_record(
        self,
        raw_company: str,
        title: str,
        raw_date: Any,
        url: str,
        source_name: str,
        remote: bool,
        role_family: Optional[RoleFamilyEnum] = None,
    ) -> Optional[JobRecord]:
        # Freshness gate: strict posting date, then text inference, then cross-run novelty for dateless entries.
        pub_date = self.check_freshness(raw_date)
        date_inferred = False
        if not pub_date and raw_date:
            # Explicit date was provided but failed 24h freshness check -> strictly stale!
            return None
        if not pub_date:
            pub_date = self.check_freshness(infer_content_freshness(title))
            date_inferred = pub_date is not None
        if not pub_date:
            norm_url = (url or "").strip()
            if not norm_url or norm_url in self._prev_run_urls:
                return None
            # Truly dateless posting never seen before: stamp collection time.
            pub_date = datetime.now(timezone.utc)
            date_inferred = True
            self._novelty_keys.add(norm_url)
            logger.debug(f"Dateless posting treated as new-since-last-run: '{title}' ({source_name}).")
        canonical, _ = await entity_resolver.resolve_async(raw_name=raw_company, source_url=url, entity_type="STARTUP")
        self._live_count += 1
        return JobRecord(
            source=SourceMetadata(name=source_name, url=url),
            content=JobContent(
                company=canonical,
                title=title,
                date=pub_date,
                is_remote=remote,
                role_family=role_family if role_family is not None else classify_role_family(title),
                date_inferred=date_inferred,
            ),
        )

    async def _collect_matching(
        self,
        records: List[JobRecord],
        items: Iterable[Any],
        mapper: Callable[[Any], dict],
        source_name: str,
        *,
        seen: Optional[set] = None,
        key_fn: Optional[Callable[[Any], str]] = None,
    ) -> Tuple[int, int]:
        """Append AI-filter-matching records built by mapper(item) to records.
        mapper returns _build_record kwargs (minus source_name). With key_fn/seen,
        items with falsy or already-seen keys are skipped, and keys are registered
        only for records that survive the freshness gate.
        Returns (survived_ai_filter, appended)."""
        survived = 0
        appended = 0
        for item in items:
            key = key_fn(item) if key_fn else None
            if seen is not None and (not key or key in seen):
                continue
            kwargs = mapper(item)
            if not AI_KEYWORD_PATTERN.search(kwargs.get("title", "")):
                continue
            survived += 1
            rec = await self._build_record(source_name=source_name, **kwargs)
            if rec:
                records.append(rec)
                appended += 1
                if seen is not None and key:
                    seen.add(key)
        return survived, appended

    async def fetch_remoteok(self) -> List[JobRecord]:
        records: List[JobRecord] = []
        try:
            items = await self.fetch_json(settings.jobs_remoteok_url)
            items = items[1:] if isinstance(items, list) and items and "legal" in str(items[0]) else (items or [])

            def _map(item):
                pos = item.get("position") or ""
                tags = item.get("tags") or []
                loc = f"{item.get('location', '')} {item.get('region', '')}"
                return {
                    "raw_company": item.get("company") or "Unknown",
                    "title": pos,
                    "raw_date": item.get("date"),
                    "url": item.get("url") or f"https://remoteok.com/jobs/{item.get('id', '')}",
                    "remote": self._is_remote(location=loc, tags=tags, title=pos),
                }

            survived_filter, _ = await self._collect_matching(records, items, _map, "RemoteOK AI")
            logger.info(f"[RemoteOK] Total items: {len(items)}, survived AI title filter: {survived_filter}, fresh (<24h): {len(records)}")
        except Exception as exc:
            logger.warning(f"RemoteOK fetch error: {exc}")
        return records

    async def fetch_arbeitnow(self) -> List[JobRecord]:
        records: List[JobRecord] = []
        try:
            data = await self.fetch_json(settings.jobs_arbeitnow_url)
            items = [i for i in (data or {}).get("data", []) if "arbeitnow.co.uk" not in i.get("url", "")]

            def _map(item):
                title = item.get("title", "")
                loc = item.get("location", "")
                tags = item.get("tags", [])
                return {
                    "raw_company": item.get("company_name", "Unknown"),
                    "title": title,
                    "raw_date": item.get("created_at"),
                    "url": item.get("url", ""),
                    "remote": bool(item.get("remote", False)) or self._is_remote(location=loc, tags=tags, title=title),
                }

            await self._collect_matching(records, items, _map, "Arbeitnow AI Jobs")
        except Exception as exc:
            logger.warning(f"Arbeitnow fetch error: {exc}")
        return records

    @staticmethod
    def _split_company_title(raw_title: str, author: str = "", split_colon: bool = False) -> Tuple[str, str]:
        """Split 'Company: Job Title' or 'Company is hiring ...' feed titles into (company, title)."""
        if split_colon and ":" in raw_title:
            company, title = [p.strip() for p in raw_title.split(":", 1)]
        elif "is hiring" in raw_title:
            company = raw_title.split("is hiring")[0].strip()
            title = raw_title
        else:
            company = author or "AI Startup"
            title = raw_title
        return company, title

    async def _fetch_feed_jobs(self, url: str, source_name: str, split_colon: bool = False) -> List[JobRecord]:
        try:
            entries = await self.fetch_feed(url)
            records: List[JobRecord] = []

            def _map(entry):
                raw_title = getattr(entry, "title", "")
                company, title = self._split_company_title(
                    raw_title,
                    author=getattr(entry, "author", "") or getattr(entry, "company", ""),
                    split_colon=split_colon,
                )
                return {
                    "raw_company": company,
                    "title": title,
                    "raw_date": getattr(entry, "published", None),
                    "url": getattr(entry, "link", ""),
                    "remote": self._is_remote(location=getattr(entry, "location", ""), title=title),
                }

            await self._collect_matching(records, entries, _map, source_name)
            return records
        except Exception as exc:
            logger.warning(f"{source_name} fetch error: {exc}")
            return []

    async def fetch_himalayas(self) -> List[JobRecord]:
        records: List[JobRecord] = []
        try:
            data = await self.fetch_json(settings.jobs_himalayas_api_url)

            def _map(job):
                return {
                    "raw_company": job.get("companyName") or "Himalayas Tech",
                    "title": job.get("title", ""),
                    "raw_date": job.get("pubDate"),
                    "url": job.get("applicationLink") or job.get("guid") or "",
                    "remote": True,
                }

            await self._collect_matching(records, (data or {}).get("jobs", []), _map, "Himalayas AI")
        except Exception as exc:
            logger.warning(f"Himalayas API fetch error: {exc}")
        if not records:
            records = await self._fetch_feed_jobs(settings.jobs_himalayas_rss_url, "Himalayas AI")
        return records

    async def fetch_weworkremotely(self) -> List[JobRecord]:
        records: List[JobRecord] = []
        seen_urls = set()
        urls = [u.strip() for u in settings.jobs_weworkremotely_urls.split("|") if u.strip()]
        try:
            for url in urls:
                try:
                    entries = await self.fetch_feed(url)
                except Exception as exc:
                    logger.warning(f"[WeWorkRemotely] {url} fetch error: {exc}")
                    continue

                def _map(entry):
                    raw_title = getattr(entry, "title", "")
                    company, title = self._split_company_title(
                        raw_title,
                        author=getattr(entry, "author", "") or "AI Startup",
                        split_colon=True,
                    )
                    return {
                        "raw_company": company,
                        "title": title,
                        "raw_date": getattr(entry, "published", None),
                        "url": getattr(entry, "link", ""),
                        "remote": self._is_remote(location=getattr(entry, "location", ""), title=title),
                    }

                survived, fresh = await self._collect_matching(
                    records,
                    entries,
                    _map,
                    "WeWorkRemotely AI",
                    seen=seen_urls,
                    key_fn=lambda e: getattr(e, "link", ""),
                )
                logger.info(
                    f"[WeWorkRemotely] {url} -> entries: {len(entries)}, "
                    f"survived AI filter: {survived}, fresh (<24h): {fresh}"
                )
        except Exception as exc:
            logger.warning(f"WeWorkRemotely fetch error: {exc}")
        return records

    @staticmethod
    def _parse_hn_first_line(first_line: str) -> Tuple[Optional[str], str, str]:
        """Parse a YC HN posting's first line into (company, title, location).
        Handles 'Company | Title | Location' and 'Company is the ...' formats;
        company is None in the generic branch where the caller falls back to the
        entry author."""
        if "|" in first_line:
            parts = [p.strip() for p in first_line.split("|")]
            return parts[0], (parts[1] if len(parts) > 1 else first_line), (parts[2] if len(parts) > 2 else "")
        if "is the" in first_line:
            return first_line.split("is the")[0].strip(), first_line[:100], ""
        return None, first_line[:100], ""

    async def _process_yc_hn_entry(self, entry: Any, sem: asyncio.Semaphore) -> Optional[JobRecord]:
        """Process a single YC HN job entry with optional LLM extraction."""
        desc = (getattr(entry, "summary", "") or getattr(entry, "description", "") or "")
        clean_desc = re.sub(r"<[^>]+>", "", desc).strip()
        first_line = clean_desc.split("\n")[0].strip() if clean_desc else ""
        company, title, loc = self._parse_hn_first_line(first_line)
        if company is None:
            company = getattr(entry, "author", "") or "AI Startup"
        if not AI_KEYWORD_PATTERN.search(title):
            return None
        raw_date = getattr(entry, "published", None)
        if not self.check_freshness(raw_date):
            return None
        apply_urls = re.findall(r'https?://[^\s<>"\'\)]+', desc)
        apply_url = next(
            (u for u in apply_urls if not any(d in u for d in ("ycombinator.com", "hnrss.org", "w3.org"))),
            getattr(entry, "link", ""),
        )
        is_remote = self._is_remote(location=loc, title=title)
        role_fam = classify_role_family(title)

        if len(clean_desc) >= 40:
            async with sem:
                try:
                    input_text = f"Job Title: {title}\nLocation: {loc}\nDescription:\n{clean_desc[:2000]}"
                    llm_out = await llm_engine.extract_structured(
                        raw_text=input_text,
                        schema_cls=JobExtractionSchema,
                        instruction=JOB_EXTRACTION_PROMPT,
                    )
                    if llm_out:
                        is_remote = llm_out.is_remote
                        if isinstance(llm_out.role_family, RoleFamilyEnum):
                            role_fam = llm_out.role_family
                except Exception as exc:
                    logger.debug(f"LLM job extraction fallback for '{title}': {exc}")

        return await self._build_record(
            raw_company=company,
            title=title,
            raw_date=raw_date,
            url=apply_url,
            source_name="YC HN Who Is Hiring AI",
            remote=is_remote,
            role_family=role_fam,
        )

    async def fetch_yc_hn_jobs(self) -> List[JobRecord]:
        """Fetch and extract YC HN Who Is Hiring jobs with concurrent LLM extraction."""
        try:
            entries = await self.fetch_feed(settings.jobs_hnrss_url)
            sem = asyncio.Semaphore(5)
            tasks = [self._process_yc_hn_entry(entry, sem) for entry in entries]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for r in results:
                if isinstance(r, Exception):
                    logger.warning(f"YC HN entry processing failed: {r!r}")
            return [r for r in results if isinstance(r, JobRecord)]
        except Exception as exc:
            logger.warning(f"YC HN fetch error: {exc}")
            return []

    async def crawl(self) -> List[JobRecord]:
        """Crawl 5 AI job boards concurrently with 24h freshness enforcement."""
        logger.info("Starting JobsCrawler across 5 AI job boards...")
        board_tasks = [
            (self.fetch_remoteok(), "RemoteOK AI"),
            (self.fetch_arbeitnow(), "Arbeitnow AI Jobs"),
            (self.fetch_himalayas(), "Himalayas AI"),
            (self.fetch_weworkremotely(), "WeWorkRemotely AI"),
            (self.fetch_yc_hn_jobs(), "YC HN Who Is Hiring AI"),
        ]
        batches = await asyncio.gather(*[task for task, _ in board_tasks], return_exceptions=True)
        records = []
        freshness = {}
        for (_, source_name), batch in zip(board_tasks, batches):
            if isinstance(batch, Exception):
                logger.warning(f"Job board fetch failed: {batch!r}")
                freshness[source_name] = 0
            else:
                records.extend(batch)
                freshness[source_name] = len(batch)

        # Persist this run's keys for the next run's novelty heuristic.
        self._novelty_keys.update(rec.source.url for rec in records)
        save_seen_keys("jobs", self._novelty_keys)

        # Per-source freshness stamps for staleness detection across runs.
        save_source_freshness("jobs", freshness)

        logger.info(f"Completed JobsCrawler: {len(records)} fresh job postings (<24h) collected.")
        return records
