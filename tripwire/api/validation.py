"""URL validation utilities for TripWire endpoints."""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

from tripwire.config.settings import settings


def validate_endpoint_url(url: str) -> str:
    """Validate an endpoint URL for safety and correctness.

    - Must be a valid URL with scheme and hostname.
    - Scheme must be HTTPS (HTTP allowed only when APP_ENV=development).
    - Blocks localhost, loopback, link-local, and private IP ranges.

    Returns the URL unchanged if valid; raises ValueError otherwise.
    """
    parsed = urlparse(url)

    # ── Scheme check ──────────────────────────────────────────
    allowed_schemes = {"https"}
    if settings.app_env == "development":
        allowed_schemes.add("http")

    if parsed.scheme not in allowed_schemes:
        if parsed.scheme == "http":
            raise ValueError(
                "HTTP endpoints are not allowed in production. Use HTTPS."
            )
        raise ValueError(
            f"Invalid URL scheme '{parsed.scheme}'. Only {', '.join(sorted(allowed_schemes))} allowed."
        )

    # ── Hostname check ────────────────────────────────────────
    hostname = parsed.hostname
    if not hostname:
        raise ValueError("URL must include a valid hostname.")

    # Block well-known local hostnames
    # Note: urlparse strips brackets from IPv6, so "::1" not "[::1]"
    blocked_hostnames = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}
    if hostname in blocked_hostnames:
        raise ValueError(
            f"Loopback/local addresses are not allowed: {hostname}"
        )

    # ── IP address checks ────────────────────────────────────
    try:
        addr = ipaddress.ip_address(hostname)
    except ValueError:
        # Not a bare IP — try resolving the hostname to catch DNS-based
        # bypasses (e.g. a domain that resolves to 127.0.0.1).  If DNS
        # resolution fails we let it pass — the URL may be valid but
        # unreachable at validation time.
        try:
            resolved = socket.getaddrinfo(hostname, None)
            for family, _type, _proto, _canon, sockaddr in resolved:
                addr = ipaddress.ip_address(sockaddr[0])
                _check_blocked_ip(addr, hostname)
        except socket.gaierror:
            pass  # DNS failure — allow; delivery will fail later
        return url

    _check_blocked_ip(addr, hostname)
    return url


def _check_blocked_ip(addr: ipaddress.IPv4Address | ipaddress.IPv6Address, hostname: str) -> None:
    """Raise ValueError if *addr* falls in a blocked range.

    Also unwraps IPv4-mapped IPv6 addresses (e.g. ``::ffff:127.0.0.1``)
    so that the inner IPv4 address is checked against all blocked ranges.
    """
    # Unwrap IPv4-mapped IPv6 (e.g. ::ffff:10.0.0.1) to check the real IPv4
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped

    if addr.is_loopback:
        raise ValueError(f"Loopback addresses are not allowed: {hostname}")
    if addr.is_private:
        raise ValueError(f"Private IP addresses are not allowed: {hostname}")
    if addr.is_link_local:
        raise ValueError(f"Link-local addresses are not allowed: {hostname}")
    if addr.is_reserved:
        raise ValueError(f"Reserved IP addresses are not allowed: {hostname}")
