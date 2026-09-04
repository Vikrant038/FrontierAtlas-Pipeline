"""
Security and SSRF (Server-Side Request Forgery) prevention utilities.
Enforces GUARDRAILS.md Module 2.7 & 6.3 controls on all outbound web requests.
"""

import ipaddress
import socket
from urllib.parse import urlparse


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
        return False  # Target is a domain name, not an IP literal


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

    # Check if hostname itself is a forbidden IP literal
    if is_ip_blocked(hostname):
        raise SSRFValidationError(f"Access to private/internal IP address '{hostname}' is blocked.")

    # Resolve hostname via DNS to prevent DNS rebinding attacks
    if resolve_dns:
        try:
            for entry in socket.getaddrinfo(hostname, None):
                if is_ip_blocked(entry[4][0]):
                    raise SSRFValidationError(f"Hostname '{hostname}' resolves to forbidden IP '{entry[4][0]}'.")
        except socket.gaierror as exc:
            raise SSRFValidationError(f"Failed to resolve DNS for hostname '{hostname}': {exc}") from exc

    return parsed.geturl()
