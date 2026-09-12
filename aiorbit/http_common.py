"""
HTTP headers and security utilities for AIOrbit (Stage 2).
Fully self-contained; zero dependencies on src/.
"""

import ipaddress
import re
import socket
from typing import Dict, Optional
from urllib.parse import urlparse
from bs4 import BeautifulSoup

DEFAULT_HEADERS: Dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"macOS"',
}


def github_headers(token: Optional[str]) -> Dict[str, str]:
    """Build GitHub REST API v3 headers, authenticating when token is present."""
    headers = {
        "User-Agent": "AIOrbit-MCP-Pipeline/1.0",
        "Accept": "application/vnd.github.v3+json",
    }
    if token:
        headers["Authorization"] = f"token {token}"
    return headers


class SSRFValidationError(ValueError):
    """Raised when an outbound URL violates SSRF security boundaries."""
    pass


def is_ip_blocked(target: str) -> bool:
    """Return True if target is a private, loopback, link-local, or reserved IP."""
    try:
        ip = ipaddress.ip_address(target)
        if isinstance(ip, ipaddress.IPv6Address) and ip in ipaddress.IPv6Network("64:ff9b::/96"):
            ip = ipaddress.IPv4Address(ip.packed[-4:])
        return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
    except ValueError:
        return False


def validate_url_safe(url: str, resolve_dns: bool = True) -> str:
    """Validate that an outbound URL does not target internal infrastructure or cloud metadata."""
    if not url or not isinstance(url, str):
        raise SSRFValidationError("URL must be a non-empty string.")

    parsed = urlparse(url.strip())
    if parsed.scheme.lower() not in ("http", "https"):
        raise SSRFValidationError(f"Invalid URL scheme '{parsed.scheme}'. Only http and https are permitted.")

    hostname = (parsed.hostname or "").lower()
    if not hostname:
        raise SSRFValidationError("URL lacks a valid hostname.")

    if hostname in ("localhost", "localhost.localdomain", "broadcasthost"):
        raise SSRFValidationError(f"Access to loopback hostname '{hostname}' is blocked.")

    if is_ip_blocked(hostname):
        raise SSRFValidationError(f"Access to private/internal IP address '{hostname}' is blocked.")

    if resolve_dns:
        try:
            for entry in socket.getaddrinfo(hostname, None):
                if is_ip_blocked(entry[4][0]):
                    raise SSRFValidationError(f"Hostname '{hostname}' resolves to forbidden IP '{entry[4][0]}'.")
        except socket.gaierror as exc:
            raise SSRFValidationError(f"Failed to resolve DNS for hostname '{hostname}': {exc}") from exc

    return parsed.geturl()
 
 
def extract_detail_page_data(html: str) -> Dict[str, Optional[str]]:
    """Extract fields from Creati.ai detail page HTML."""
    soup = BeautifulSoup(html, "html.parser")

    # 1. Clean product name
    h1 = soup.find("h1")
    title_text = h1.get_text(strip=True) if h1 else None
    if not title_text and soup.title:
        title_text = soup.title.string.split("|")[0].strip()

    # 2. Description
    desc = None
    meta_desc = soup.find("meta", attrs={"name": "description"})
    if meta_desc and meta_desc.get("content"):
        desc = meta_desc["content"].strip()
    if not desc:
        p = soup.find("p")
        if p:
            desc = p.get_text(strip=True)

    # 3. Category breadcrumbs
    category = None
    cat_links = soup.find_all("a", href=re.compile(r"/mcp/categories/([a-zA-Z0-9_.-]+)"))
    if cat_links:
        category = cat_links[0].get_text(strip=True)

    # 4. Pricing
    pricing = "Free"
    text_content = soup.get_text()
    p_match = re.search(r" (Free|Freemium|Paid) ", text_content, re.I)
    if p_match:
        pricing = p_match.group(1).title()

    # 5. On-page stars
    stars_on_page = None
    s_match = re.search(r"(\d+)\s*Stars", text_content, re.I)
    if s_match:
        try:
            stars_on_page = int(s_match.group(1))
        except ValueError:
            pass

    # 6. GitHub URL resolution (first valid match wins, filtering .github noise)
    github_url = None
    github_owner_repo = None
    official_url = None

    all_anchors = soup.find_all("a", href=True)
    # Priority A: Check for Visit MCP or Stars buttons
    for a in all_anchors:
        href = a["href"].strip()
        if "github.com/" in href:
            m = re.search(r"github\.com/([a-zA-Z0-9_.-]+)/([a-zA-Z0-9_.-]+)", href)
            if m:
                owner, repo = m.group(1), m.group(2)
                repo = repo.split("#")[0].split("?")[0].rstrip("/")
                if repo.endswith(".git"):
                    repo = repo[:-4]
                if repo.lower() not in (".github", "profile") and owner.lower() != repo.lower():
                    github_owner_repo = f"{owner}/{repo}"
                    github_url = f"https://github.com/{owner}/{repo}"
                    break

    # If not found yet, check any github link
    if not github_owner_repo:
        for a in all_anchors:
            href = a["href"].strip()
            if "github.com/" in href:
                m = re.search(r"github\.com/([a-zA-Z0-9_.-]+)/([a-zA-Z0-9_.-]+)", href)
                if m:
                    owner, repo = m.group(1), m.group(2)
                    if repo.lower() != ".github":
                        github_owner_repo = f"{owner}/{repo}"
                        github_url = f"https://github.com/{owner}/{repo}"
                        break

    # Check for external official site (non-creati, non-github, non-social)
    for a in all_anchors:
        href = a["href"].strip()
        if href.startswith("http") and not any(
            d in href for d in ("creati.ai", "github.com", "twitter.com", "x.com", "linkedin.com", "youtube.com")
        ):
            official_url = href
            break

    return {
        "name": title_text,
        "description": desc,
        "category": category,
        "pricing": pricing,
        "stars_on_page": stars_on_page,
        "github_url": github_url,
        "github_owner_repo": github_owner_repo,
        "official_url": official_url,
    }
