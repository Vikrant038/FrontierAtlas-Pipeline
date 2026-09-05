"""
Shared verification helpers for the verification scripts.
- load_csv: read a deliverable CSV into (headers, rows)
- URL-liveness audit: spot-checks that collected source URLs are still reachable,
  escalating through HEAD and curl-cffi TLS impersonation on 403/405 anti-bot gates.
Used by verify_all.py, verify_phase1.py, and verify_phase2.py.
"""

import asyncio
import csv
from pathlib import Path
from typing import Dict, List, Tuple

import httpx
from curl_cffi.requests import Session as CurlSession

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def load_csv(filepath: Path) -> Tuple[List[str], List[Dict[str, str]]]:
    """Read headers and rows from a CSV file; (empty, empty) when missing or headerless."""
    if not filepath.exists():
        return [], []
    with open(filepath, "r", encoding="utf-8") as f:
        reader = csv.reader(f)
        try:
            headers = next(reader)
        except StopIteration:
            return [], []
        dict_reader = csv.DictReader(f, fieldnames=headers)
        return headers, list(dict_reader)


async def probe_url(client: httpx.AsyncClient, sem: asyncio.Semaphore, url: str) -> Tuple[str, int, str]:
    """Test one URL: httpx GET, then HEAD, then curl-cffi TLS impersonation on 403/405."""
    if not url or not url.startswith("http"):
        return url, 0, "Invalid URL"
    async with sem:
        try:
            resp = await client.get(url)
            if resp.status_code == 200:
                return url, 200, "OK"
            if resp.status_code in (403, 405):
                try:
                    head_resp = await client.head(url)
                    if head_resp.status_code == 200:
                        return url, 200, "OK (HEAD)"
                except Exception:
                    pass
                try:
                    with CurlSession(impersonate="chrome124") as curl_s:
                        c_resp = curl_s.get(url, headers=BROWSER_HEADERS, timeout=8)
                        if c_resp.status_code == 200:
                            return url, 200, "OK (TLS)"
                        return url, c_resp.status_code, f"HTTP {c_resp.status_code} (TLS)"
                except Exception as exc:
                    return url, resp.status_code, f"HTTP {resp.status_code} ({exc.__class__.__name__})"
            return url, resp.status_code, f"HTTP {resp.status_code}"
        except httpx.TimeoutException:
            return url, 0, "Timeout"
        except Exception as exc:
            return url, 0, exc.__class__.__name__


async def audit_urls(
    urls: List[str],
    sample_size: int = 0,
    concurrency: int = 10,
    timeout: float = 10.0,
) -> Tuple[int, int, List[Tuple[str, int, str]]]:
    """Probe unique URLs (first ``sample_size`` when > 0) and return (passed, tested, failures)."""
    unique = list(dict.fromkeys(urls))
    if sample_size and sample_size > 0:
        unique = unique[:sample_size]
    if not unique:
        return 0, 0, []
    sem = asyncio.Semaphore(concurrency)
    async with httpx.AsyncClient(headers=BROWSER_HEADERS, follow_redirects=True, timeout=timeout) as client:
        results = await asyncio.gather(*[probe_url(client, sem, u) for u in unique])
    passed = sum(1 for _, status, _ in results if status == 200)
    non_200 = [r for r in results if r[1] != 200]
    return passed, len(unique), non_200