import asyncio
import re
from typing import Any, List, Optional

from src.crawlers.base import AsyncBaseCrawler
from src.schemas.entities import JobContent, JobRecord, RoleFamilyEnum, SourceMetadata
from src.resolution.normalizer import entity_resolver
from src.utils.logger import logger

ROLE_MAP = [
    (RoleFamilyEnum.RESEARCH, re.compile(r"research|scientist|phd|postdoc", re.I)),
    (RoleFamilyEnum.ENGINEERING, re.compile(r"engineer|developer|architect|programmer|mlops|backend", re.I)),
    (RoleFamilyEnum.PRODUCT, re.compile(r"product|pm\b", re.I)),
    (RoleFamilyEnum.DESIGN, re.compile(r"design|ui|ux", re.I)),
    (RoleFamilyEnum.SALES, re.compile(r"sales|bdr|sdr|account exec", re.I)),
    (RoleFamilyEnum.MARKETING, re.compile(r"marketing|growth", re.I)),
    (RoleFamilyEnum.OPERATIONS, re.compile(r"operations|ops|chief of staff", re.I)),
    (RoleFamilyEnum.LEGAL, re.compile(r"legal|counsel", re.I)),
    (RoleFamilyEnum.FINANCE, re.compile(r"finance|accountant|cfo", re.I)),
]


class JobsCrawler(AsyncBaseCrawler):
    """Crawler for ingesting fresh (<24h) AI job postings across 5 AI job boards."""

    @staticmethod
    def _is_remote(location: str = "", tags: Optional[List[str]] = None) -> bool:
        loc = (location or "").strip().lower()
        if not loc:
            return True
        tag_str = " ".join(tags or []).lower()
        return any(w in loc or w in tag_str for w in ("remote", "worldwide"))

    @staticmethod
    def _classify_role(title: str) -> RoleFamilyEnum:
        for role, pattern in ROLE_MAP:
            if pattern.search(title):
                return role
        return RoleFamilyEnum.OTHER

    def _build_record(self, raw_company: str, title: str, raw_date: Any, url: str, source_name: str, remote: bool) -> Optional[JobRecord]:
        pub_date = self.check_freshness(raw_date)
        if not pub_date:
            return None
        canonical, _ = entity_resolver.resolve(raw_name=raw_company, source_url=url, entity_type="STARTUP")
        return JobRecord(
            source=SourceMetadata(name=source_name, url=url),
            content=JobContent(company=canonical, title=title, date=pub_date, is_remote=remote, role_family=self._classify_role(title)),
        )

    async def fetch_remoteok(self) -> List[JobRecord]:
        try:
            items = await self.fetch_json("https://remoteok.com/api?tag=ai")
            items = items[1:] if isinstance(items, list) and items and "legal" in str(items[0]) else (items or [])
            records: List[JobRecord] = []
            for item in items:
                loc = f"{item.get('location', '')} {item.get('region', '')}"
                is_remote = self._is_remote(loc, item.get("tags"))
                rec = self._build_record(
                    raw_company=item.get("company") or "Unknown",
                    title=item.get("position") or "AI Specialist",
                    raw_date=item.get("date"),
                    url=item.get("url") or f"https://remoteok.com/jobs/{item.get('id', '')}",
                    source_name="RemoteOK AI",
                    remote=is_remote,
                )
                if rec:
                    records.append(rec)
            return records
        except Exception as exc:
            logger.warning(f"RemoteOK fetch error: {exc}")
            return []

    async def fetch_arbeitnow(self) -> List[JobRecord]:
        try:
            data = await self.fetch_json("https://www.arbeitnow.com/api/job-board-api")
            records: List[JobRecord] = []
            for item in (data or {}).get("data", []):
                title = item.get("title", "")
                if not any(k in title.lower() for k in ("ai", "machine learning", "data", "llm")):
                    continue
                rec = self._build_record(
                    raw_company=item.get("company_name", "Unknown"),
                    title=title,
                    raw_date=item.get("created_at"),
                    url=item.get("url", ""),
                    source_name="Arbeitnow AI Jobs",
                    remote=item.get("remote", False),
                )
                if rec:
                    records.append(rec)
            return records
        except Exception as exc:
            logger.warning(f"Arbeitnow fetch error: {exc}")
            return []

    async def _fetch_feed_jobs(self, url: str, source_name: str, split_colon: bool = False) -> List[JobRecord]:
        try:
            entries = await self.fetch_feed(url)
            records: List[JobRecord] = []
            for entry in entries:
                raw_title = getattr(entry, "title", "")
                if not any(k in raw_title.lower() for k in ("ai", "machine learning", "ml", "llm", "data")):
                    continue
                if split_colon and ":" in raw_title:
                    company, title = [p.strip() for p in raw_title.split(":", 1)]
                elif "is hiring" in raw_title:
                    company = raw_title.split("is hiring")[0].strip()
                    title = raw_title
                else:
                    company = getattr(entry, "author", "") or getattr(entry, "company", "AI Startup")
                    title = raw_title
                rec = self._build_record(
                    raw_company=company,
                    title=title,
                    raw_date=getattr(entry, "published", None),
                    url=getattr(entry, "link", ""),
                    source_name=source_name,
                    remote=True,
                )
                if rec:
                    records.append(rec)
            return records
        except Exception as exc:
            logger.warning(f"{source_name} fetch error: {exc}")
            return []

    async def fetch_himalayas(self) -> List[JobRecord]:
        return await self._fetch_feed_jobs("https://himalayas.app/jobs/rss", "Himalayas AI")

    async def fetch_weworkremotely(self) -> List[JobRecord]:
        return await self._fetch_feed_jobs(
            "https://weworkremotely.com/categories/remote-programming-jobs.rss", "WeWorkRemotely AI", split_colon=True
        )

    async def fetch_yc_hn_jobs(self) -> List[JobRecord]:
        return await self._fetch_feed_jobs("https://hnrss.org/whoishiring/jobs?q=AI", "YC HN Who Is Hiring AI")

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
        logger.info(f"Completed JobsCrawler: {len(records)} fresh job postings (<24h) collected.")
        return records
