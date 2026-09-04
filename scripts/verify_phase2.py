"""
Phase II Verification & Freshness Audit Script.
Audits Phase II CSV exports (jobs.csv and news.csv) against strict specifications:
- Verifies Schema Conformance: CSV headers match JobRecord / NewsRecord field specs exactly.
- Verifies Record Counts: Reports total records and breakdown by monitored source.
- Verifies 24-Hour Freshness: Asserts 100% of signals are within 24h of collection timestamp.
- Verifies URL Liveness: Validates that source URLs return HTTP 200 (browser UA, 10s timeout,
  curl-cffi TLS fallback, tolerating max 3 non-200 paywalled sites, requiring >= 95% liveness).

Usage:
    python scripts/verify_phase2.py [path/to/jobs.csv] [path/to/news.csv]
    (defaults to exports/jobs.csv and exports/news.csv)
"""

import asyncio
import csv
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

# Ensure repository root is in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
from curl_cffi.requests import Session as CurlSession
from dateutil import parser as dateutil_parser

from src.exporters.base import ENTITY_SPECS

DEFAULT_JOBS_PATH = Path("exports/jobs.csv")
DEFAULT_NEWS_PATH = Path("exports/news.csv")

JOBS_PATH = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_JOBS_PATH
NEWS_PATH = Path(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_NEWS_PATH

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def load_csv_rows(filepath: Path) -> Tuple[List[str], List[Dict[str, str]]]:
    """Load headers and rows from a CSV file."""
    if not filepath.exists():
        print(f"❌ Error: {filepath} not found.")
        return [], []
    with open(filepath, "r", encoding="utf-8") as f:
        reader = csv.reader(f)
        try:
            headers = next(reader)
        except StopIteration:
            return [], []
        dict_reader = csv.DictReader(f, fieldnames=headers)
        return headers, list(dict_reader)


def parse_utc_dt(raw: Optional[str]) -> Optional[datetime]:
    """Parse ISO date string into UTC datetime."""
    if not raw:
        return None
    try:
        dt = dateutil_parser.parse(raw.strip())
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
    except Exception:
        return None


async def probe_url(client: httpx.AsyncClient, sem: asyncio.Semaphore, url: str) -> Tuple[str, int, str]:
    """Test URL liveness with browser UA and curl-cffi TLS fallback on 403."""
    if not url or not url.startswith("http"):
        return url, 0, "Invalid URL schema"

    async with sem:
        try:
            resp = await client.get(url)
            if resp.status_code == 200:
                return url, 200, "OK"
            if resp.status_code in (403, 405):
                # 1. Test HEAD request (works for bot-gated pages like Himalayas that accept HEAD)
                try:
                    head_resp = await client.head(url)
                    if head_resp.status_code == 200:
                        return url, 200, "OK (HEAD)"
                except Exception:
                    pass
                # 2. Attempt socket-level TLS impersonation via curl-cffi
                try:
                    with CurlSession(impersonate="chrome124") as curl_s:
                        c_resp = curl_s.get(url, headers=BROWSER_HEADERS, timeout=10)
                        if c_resp.status_code == 200:
                            return url, 200, "OK (TLS fallback)"
                        return url, c_resp.status_code, f"HTTP {c_resp.status_code} (TLS)"
                except Exception as c_exc:
                    return url, resp.status_code, f"HTTP {resp.status_code} ({c_exc.__class__.__name__})"
            return url, resp.status_code, f"HTTP {resp.status_code}"
        except httpx.TimeoutException:
            return url, 0, "Request Timeout (10s)"
        except Exception as exc:
            return url, 0, f"Error: {exc.__class__.__name__}"


async def audit_urls(urls: List[str]) -> Tuple[int, int, List[Tuple[str, int, str]]]:
    """Audit liveness of unique URLs concurrently."""
    unique_urls = list(dict.fromkeys(urls))
    sem = asyncio.Semaphore(12)
    async with httpx.AsyncClient(headers=BROWSER_HEADERS, follow_redirects=True, timeout=10.0) as client:
        tasks = [probe_url(client, sem, u) for u in unique_urls]
        results = await asyncio.gather(*tasks)

    passed = 0
    non_200 = []
    for u, status, msg in results:
        if status == 200:
            passed += 1
        else:
            non_200.append((u, status, msg))

    return passed, len(unique_urls), non_200


def main() -> None:
    print("=" * 75)
    print("🔍 PHASE II AUDIT & VERIFICATION ENGINE (24h Freshness & Liveness)")
    print("=" * 75)
    print(f"Jobs CSV : {JOBS_PATH}")
    print(f"News CSV : {NEWS_PATH}\n")

    jobs_headers, jobs_rows = load_csv_rows(JOBS_PATH)
    news_headers, news_rows = load_csv_rows(NEWS_PATH)

    if not jobs_rows and not news_rows:
        print("❌ Fatal: Both jobs.csv and news.csv are missing or empty.")
        sys.exit(1)

    all_passed = True

    # 1. Schema Conformance Verification
    print("--- 1. Schema Conformance Audit ---")
    expected_jobs_headers = ENTITY_SPECS["jobs"][2]
    expected_news_headers = ENTITY_SPECS["news"][2]

    jobs_schema_ok = (jobs_headers == expected_jobs_headers)
    news_schema_ok = (news_headers == expected_news_headers)

    print(f"  Jobs CSV Headers Match Spec : {'✅ PASS' if jobs_schema_ok else '❌ FAIL'}")
    if not jobs_schema_ok:
        print(f"    Expected: {expected_jobs_headers}")
        print(f"    Actual  : {jobs_headers}")
        all_passed = False

    print(f"  News CSV Headers Match Spec : {'✅ PASS' if news_schema_ok else '❌ FAIL'}")
    if not news_schema_ok:
        print(f"    Expected: {expected_news_headers}")
        print(f"    Actual  : {news_headers}")
        all_passed = False
    print()

    # 2. Record Counts & Per-Source Distribution
    print("--- 2. Record Counts & Source Distribution ---")
    jobs_by_source = Counter(r.get("source.name", "Unknown") for r in jobs_rows)
    news_by_source = Counter(r.get("source.name", "Unknown") for r in news_rows)

    print(f"  Total Jobs Collected : {len(jobs_rows)} across {len(jobs_by_source)} boards")
    for src, count in jobs_by_source.most_common():
        print(f"    • {src:30s}: {count:3d} listings")

    print(f"\n  Total News Collected : {len(news_rows)} across {len(news_by_source)} publishers")
    for src, count in news_by_source.most_common():
        print(f"    • {src:30s}: {count:3d} articles")
    print()

    # 3. 24-Hour Freshness Audit
    print("--- 3. 24-Hour Freshness Guarantee Audit ---")
    fresh_jobs = 0
    stale_jobs = []
    for idx, r in enumerate(jobs_rows, start=1):
        dt_post = parse_utc_dt(r.get("content.date"))
        dt_col = parse_utc_dt(r.get("collectedAt"))
        if not dt_post or not dt_col:
            stale_jobs.append((idx, r.get("source.url"), "Unparseable timestamp"))
            continue
        age_hours = (dt_col - dt_post).total_seconds() / 3600.0
        if -0.08 <= age_hours <= 24.0:
            fresh_jobs += 1
        else:
            stale_jobs.append((idx, r.get("source.url"), f"{age_hours:.2f}h old"))

    fresh_news = 0
    stale_news = []
    for idx, r in enumerate(news_rows, start=1):
        dt_pub = parse_utc_dt(r.get("content.published_date"))
        dt_col = parse_utc_dt(r.get("collectedAt"))
        if not dt_pub or not dt_col:
            stale_news.append((idx, r.get("source.url"), "Unparseable timestamp"))
            continue
        age_hours = (dt_col - dt_pub).total_seconds() / 3600.0
        if -0.08 <= age_hours <= 24.0:
            fresh_news += 1
        else:
            stale_news.append((idx, r.get("source.url"), f"{age_hours:.2f}h old"))

    total_records = len(jobs_rows) + len(news_rows)
    total_fresh = fresh_jobs + fresh_news
    freshness_rate = (total_fresh / total_records * 100.0) if total_records else 0.0

    print(f"  Jobs Freshness Compliance : {fresh_jobs}/{len(jobs_rows)} fresh "
          f"({'✅ 100%' if fresh_jobs == len(jobs_rows) else '❌ FAIL'})")
    if stale_jobs:
        for idx, u, reason in stale_jobs[:3]:
            print(f"    ⚠️ Stale Job #{idx}: {u} -> {reason}")

    print(f"  News Freshness Compliance : {fresh_news}/{len(news_rows)} fresh "
          f"({'✅ 100%' if fresh_news == len(news_rows) else '❌ FAIL'})")
    if stale_news:
        for idx, u, reason in stale_news[:3]:
            print(f"    ⚠️ Stale News #{idx}: {u} -> {reason}")

    print(f"  Combined Freshness Rate   : {freshness_rate:.1f}% ({total_fresh}/{total_records})")
    if total_fresh != total_records:
        print("  ❌ Freshness requirement failed: 100% freshness within 24h is strictly mandatory.")
        all_passed = False
    else:
        print("  ✅ 100% Freshness Compliance Confirmed (all signals <= 24h).")
    print()

    # 4. URL Liveness Audit
    print("--- 4. URL Liveness & Reachability Audit ---")
    urls_to_test = [r.get("source.url", "") for r in jobs_rows + news_rows if r.get("source.url")]
    passed_urls, total_unique, non_200_list = asyncio.run(audit_urls(urls_to_test))

    liveness_pct = (passed_urls / total_unique * 100.0) if total_unique else 0.0
    print(f"  Unique URLs Tested        : {total_unique}")
    print(f"  Live URLs (200 OK)        : {passed_urls}")
    print(f"  Non-200 Responses         : {len(non_200_list)}")
    print(f"  URL Liveness Rate         : {liveness_pct:.1f}% (Minimum required: 95.0%)")

    for u, status, msg in non_200_list:
        print(f"    ⚠️ Warning Non-200: [{status}] {u[:65]}... ({msg})")

    # Tolerates max 3 non-200 paywalled publications with warning
    if len(non_200_list) > 3:
        print(f"  ❌ Too many non-200 URLs ({len(non_200_list)} > 3 tolerated).")
        all_passed = False
    elif liveness_pct < 95.0:
        print(f"  ❌ URL liveness rate below 95.0% threshold ({liveness_pct:.1f}%).")
        all_passed = False
    else:
        print("  ✅ URL Liveness Threshold Satisfied (>= 95.0% live, <= 3 paywalls tolerated).")
    print()

    # 5. Final Decision
    print("=" * 75)
    if all_passed:
        print("✅ PHASE II AUDIT PASSED: 100% Freshness, Schema Conformant, Live URLs Verified.")
        print("=" * 75)
        sys.exit(0)
    else:
        print("❌ PHASE II AUDIT FAILED: Disqualification criteria triggered.")
        print("=" * 75)
        sys.exit(1)


if __name__ == "__main__":
    main()
