from __future__ import annotations

import ipaddress
from urllib.parse import urlparse

_BLOCKED_NETWORKS = [
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("192.0.0.0/24"),
    ipaddress.ip_network("192.0.2.0/24"),
    ipaddress.ip_network("198.51.100.0/24"),
    ipaddress.ip_network("203.0.113.0/24"),
    ipaddress.ip_network("224.0.0.0/4"),
]

_LOCALHOST_HOSTNAMES = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0", "[::1]"})


def _is_loopback_host(hostname: str) -> bool:
    h = hostname.lower().rstrip(".")
    return h in _LOCALHOST_HOSTNAMES


def validate_url(url: str, *, allow_localhost: bool = False, require_https: bool = True) -> str:
    """Validate a URL against SSRF restrictions.

    Returns the normalized URL on success. Raises ValueError on any violation.

    Rules:
      - Must be absolute URL with http(s) scheme
      - HTTPS required unless allow_localhost=True (for local dev)
      - Loopback hostnames blocked unless allow_localhost=True
      - Private, link-local, reserved IPs blocked
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"URL scheme must be http or https, got {parsed.scheme!r}")

    if require_https and parsed.scheme != "https":
        raise ValueError("Only HTTPS URLs are allowed outside local tests")

    if not parsed.hostname:
        raise ValueError("URL must have a hostname")

    hostname = parsed.hostname.lower()

    if _is_loopback_host(hostname) and not allow_localhost:
        raise ValueError(f"Loopback hostname blocked: {hostname}")

    try:
        ip = ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        if _is_loopback_ip(ip) and not allow_localhost:
            raise ValueError(f"Loopback IP blocked: {ip}")
        for net in _BLOCKED_NETWORKS:
            if ip in net:
                raise ValueError(f"IP {ip} is in blocked network {net}")
    return parsed.geturl()


def _is_loopback_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return ip.is_loopback or ip.is_unspecified


def validate_redirect_url(url: str, *, allow_localhost: bool = False) -> str:
    """Validate a redirect destination. Uses the same rules as validate_url."""
    return validate_url(url, allow_localhost=allow_localhost, require_https=True)


def is_private_ip(hostname: str) -> bool:
    """Check if a resolved hostname resolves to a private/reserved IP."""
    try:
        ip = ipaddress.ip_address(hostname)
    except ValueError:
        return False
    return any(ip in net for net in _BLOCKED_NETWORKS)
