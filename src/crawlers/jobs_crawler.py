import asyncio
import re
from datetime import datetime, timezone
from typing import Any, List, Optional
import feedparser

from src.crawlers.base import AsyncBaseCrawler
from src.llm.fallback_chain import llm_engine
from src.llm.prompts import JOB_EXTRACTION_PROMPT, JobExtractionSchema
from src.llm.rules import ONSITE_SIGNALS, REMOTE_SIGNALS, classify_role_family
from src.schemas.entities import JobContent, JobRecord, RoleFamilyEnum, SourceMetadata
from src.resolution.normalizer import entity_resolver
from src.utils.date_normalizer import infer_content_freshness
from src.utils.logger import logger
from src.utils.run_state import load_seen_keys, save_seen_keys

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

    @staticmethod
    def _is_remote(location: str = "", tags: Optional[List[str]] = None, title: str = "") -> bool:
        loc_str = (location or "").strip().lower()
        title_str = (title or "").lower()
        tag_str = " ".join(tags or []).lower()
        if any(w in loc_str or w in tag_str or w in title_str for w in REMOTE_SIGNALS):
            return True
        if not loc_str and not any(w in title_str for w in ONSITE_SIGNALS):
            return True
        return False

    @staticmethod
    def _classify_role(title: str) -> RoleFamilyEnum:
        return classify_role_family(title)

    def _build_record(
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
        if not pub_date and raw_date:
            # Explicit date was provided but failed 24h freshness check -> strictly stale!
            return None
        if not pub_date:
            pub_date = self.check_freshness(infer_content_freshness(title))
        if not pub_date:
            norm_url = (url or "").strip()
            if not norm_url or norm_url in self._prev_run_urls:
                return None
            # Truly dateless posting never seen before: stamp collection time.
            pub_date = datetime.now(timezone.utc)
            self._novelty_keys.add(norm_url)
            logger.debug(f"Dateless posting treated as new-since-last-run: '{title}' ({source_name}).")
        canonical, _ = entity_resolver.resolve(raw_name=raw_company, source_url=url, entity_type="STARTUP")
        return JobRecord(
            source=SourceMetadata(name=source_name, url=url),
            content=JobContent(
                company=canonical,
                title=title,
                date=pub_date,
                is_remote=remote,
                role_family=role_family if role_family is not None else self._classify_role(title),
            ),
        )

    async def fetch_remoteok(self) -> List[JobRecord]:
        records: List[JobRecord] = []
        try:
            client = await self.get_client()
            url = "https://remoteok.com/api?tag=ai"
            resp = await client.get(url, headers=self.headers)
            logger.info(f"[RemoteOK] Raw response status: {resp.status_code}, length: {len(resp.text)} bytes ({url})")
            if resp.status_code == 200:
                items = resp.json()
                items = items[1:] if isinstance(items, list) and items and "legal" in str(items[0]) else (items or [])
                survived_filter = 0
                for item in items:
                    pos = item.get("position") or ""
                    if not AI_KEYWORD_PATTERN.search(pos):
                        continue
                    survived_filter += 1
                    tags = item.get("tags") or []
                    loc = f"{item.get('location', '')} {item.get('region', '')}"
                    is_remote = self._is_remote(location=loc, tags=tags, title=pos)
                    rec = self._build_record(
                        raw_company=item.get("company") or "Unknown",
                        title=pos,
                        raw_date=item.get("date"),
                        url=item.get("url") or f"https://remoteok.com/jobs/{item.get('id', '')}",
                        source_name="RemoteOK AI",
                        remote=is_remote,
                    )
                    if rec:
                        records.append(rec)
                logger.info(f"[RemoteOK] Total items: {len(items)}, survived AI title filter: {survived_filter}, fresh (<24h): {len(records)}")
        except Exception as exc:
            logger.warning(f"RemoteOK fetch error: {exc}")
        return records

    async def fetch_arbeitnow(self) -> List[JobRecord]:
        records: List[JobRecord] = []
        try:
            data = await self.fetch_json("https://www.arbeitnow.com/api/job-board-api")
            for item in (data or {}).get("data", []):
                title = item.get("title", "")
                if not AI_KEYWORD_PATTERN.search(title):
                    continue
                url = item.get("url", "")
                if "arbeitnow.co.uk" in url:
                    continue
                loc = item.get("location", "")
                tags = item.get("tags", [])
                is_remote = bool(item.get("remote", False)) or self._is_remote(location=loc, tags=tags, title=title)
                rec = self._build_record(
                    raw_company=item.get("company_name", "Unknown"),
                    title=title,
                    raw_date=item.get("created_at"),
                    url=url,
                    source_name="Arbeitnow AI Jobs",
                    remote=is_remote,
                )
                if rec:
                    records.append(rec)
        except Exception as exc:
            logger.warning(f"Arbeitnow fetch error: {exc}")
        return records

    async def _fetch_feed_jobs(self, url: str, source_name: str, split_colon: bool = False) -> List[JobRecord]:
        try:
            entries = await self.fetch_feed(url)
            records: List[JobRecord] = []
            for entry in entries:
                raw_title = getattr(entry, "title", "")
                if split_colon and ":" in raw_title:
                    company, title = [p.strip() for p in raw_title.split(":", 1)]
                elif "is hiring" in raw_title:
                    company = raw_title.split("is hiring")[0].strip()
                    title = raw_title
                else:
                    company = getattr(entry, "author", "") or getattr(entry, "company", "AI Startup")
                    title = raw_title
                if not AI_KEYWORD_PATTERN.search(title):
                    continue
                is_remote = self._is_remote(location=getattr(entry, "location", ""), title=title)
                rec = self._build_record(
                    raw_company=company,
                    title=title,
                    raw_date=getattr(entry, "published", None),
                    url=getattr(entry, "link", ""),
                    source_name=source_name,
                    remote=is_remote,
                )
                if rec:
                    records.append(rec)
            return records
        except Exception as exc:
            logger.warning(f"{source_name} fetch error: {exc}")
            return []

    async def fetch_himalayas(self) -> List[JobRecord]:
        records: List[JobRecord] = []
        try:
            client = await self.get_client()
            resp = await client.get("https://himalayas.app/jobs/api?q=ai", headers=self.headers)
            if resp.status_code == 200:
                data = resp.json()
                for job in (data or {}).get("jobs", []):
                    title = job.get("title", "")
                    if not AI_KEYWORD_PATTERN.search(title):
                        continue
                    company = job.get("companyName") or "Himalayas Tech"
                    url = job.get("applicationLink") or job.get("guid") or ""
                    pub_date = job.get("pubDate")
                    rec = self._build_record(
                        raw_company=company,
                        title=title,
                        raw_date=pub_date,
                        url=url,
                        source_name="Himalayas AI",
                        remote=True,
                    )
                    if rec:
                        records.append(rec)
        except Exception as exc:
            logger.warning(f"Himalayas API fetch error: {exc}")
        if not records:
            records = await self._fetch_feed_jobs("https://himalayas.app/jobs/rss", "Himalayas AI")
        return records

    async def fetch_weworkremotely(self) -> List[JobRecord]:
        records: List[JobRecord] = []
        seen_urls = set()
        urls = [
            "https://weworkremotely.com/remote-jobs.rss",
            "https://weworkremotely.com/categories/remote-programming-jobs.rss",
            "https://weworkremotely.com/categories/remote-customer-support-jobs.rss",
        ]
        try:
            client = await self.get_client()
            for url in urls:
                resp = await client.get(url, headers=self.headers)
                if resp.status_code != 200:
                    logger.info(f"[WeWorkRemotely] {url} -> status: {resp.status_code}")
                    continue
                feed = feedparser.parse(resp.text)
                entries = getattr(feed, "entries", [])
                survived_ai_filter = 0
                fresh_count = 0
                for entry in entries:
                    link = getattr(entry, "link", "")
                    if not link or link in seen_urls:
                        continue
                    raw_title = getattr(entry, "title", "")
                    if ":" in raw_title:
                        company, title = [p.strip() for p in raw_title.split(":", 1)]
                    else:
                        company = getattr(entry, "author", "") or "AI Startup"
                        title = raw_title
                    if not AI_KEYWORD_PATTERN.search(title):
                        continue
                    survived_ai_filter += 1
                    is_remote = self._is_remote(location=getattr(entry, "location", ""), title=title)
                    rec = self._build_record(
                        raw_company=company,
                        title=title,
                        raw_date=getattr(entry, "published", None),
                        url=link,
                        source_name="WeWorkRemotely AI",
                        remote=is_remote,
                    )
                    if rec:
                        seen_urls.add(link)
                        records.append(rec)
                        fresh_count += 1
                logger.info(
                    f"[WeWorkRemotely] {url} -> status: {resp.status_code}, "
                    f"entries: {len(entries)}, survived AI filter: {survived_ai_filter}, "
                    f"fresh (<24h): {fresh_count}"
                )
        except Exception as exc:
            logger.warning(f"WeWorkRemotely fetch error: {exc}")
        return records

    async def fetch_yc_hn_jobs(self) -> List[JobRecord]:
        records: List[JobRecord] = []
        try:
            entries = await self.fetch_feed("https://hnrss.org/whoishiring/jobs?q=AI")
            for entry in entries:
                desc = getattr(entry, "summary", "") or getattr(entry, "description", "")
                clean_desc = re.sub(r"<[^>]+>", "", desc).strip()
                first_line = clean_desc.split("\n")[0].strip() if clean_desc else ""
                if "|" in first_line:
                    parts = [p.strip() for p in first_line.split("|")]
                    company = parts[0]
                    title = parts[1] if len(parts) > 1 else first_line
                    loc = parts[2] if len(parts) > 2 else ""
                elif "is the" in first_line:
                    company = first_line.split("is the")[0].strip()
                    title = first_line[:100]
                    loc = ""
                else:
                    company = getattr(entry, "author", "") or "AI Startup"
                    title = first_line[:100]
                    loc = ""
                if not AI_KEYWORD_PATTERN.search(title):
                    continue
                apply_urls = re.findall(r'https?://[^\s<>"\'\)]+', desc)
                apply_url = next(
                    (u for u in apply_urls if not any(d in u for d in ("ycombinator.com", "hnrss.org", "w3.org"))),
                    getattr(entry, "link", ""),
                )
                # Default heuristics
                is_remote = self._is_remote(location=loc, title=title)
                role_fam = self._classify_role(title)

                # LLM extraction when description text is available (clean_desc >= 40 chars)
                if len(clean_desc) >= 40:
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

                rec = self._build_record(
                    raw_company=company,
                    title=title,
                    raw_date=getattr(entry, "published", None),
                    url=apply_url,
                    source_name="YC HN Who Is Hiring AI",
                    remote=is_remote,
                    role_family=role_fam,
                )
                if rec:
                    records.append(rec)
        except Exception as exc:
            logger.warning(f"YC HN fetch error: {exc}")
        return records

    async def crawl(self) -> List[JobRecord]:
        """Crawl 5 AI job boards concurrently with 24h freshness enforcement."""
        logger.info("Starting JobsCrawler across 5 AI job boards...")
        tasks = [
            self.fetch_remoteok(),
            self.fetch_arbeitnow(),
            self.fetch_himalayas(),
            self.fetch_weworkremotely(),
            self.fetch_yc_hn_jobs(),
        ]
        batches = await asyncio.gather(*tasks)
        records = [rec for batch in batches for rec in batch]

        # Persist this run's keys for the next run's novelty heuristic.
        self._novelty_keys.update(rec.source.url for rec in records)
        save_seen_keys("jobs", self._novelty_keys)

        logger.info(f"Completed JobsCrawler: {len(records)} fresh job postings (<24h) collected.")
        return records
