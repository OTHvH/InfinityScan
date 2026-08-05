"""Strict, opt-in parsing of proxy forwarding headers."""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Mapping


_FORWARDED_FOR_RE = re.compile(r"for=(?:\"([^\"]+)\"|([^;,\s]+))", re.IGNORECASE)


def _parse_ip(value: str) -> str | None:
    candidate = value.strip().strip('"')
    if candidate.startswith("[") and "]" in candidate:
        candidate = candidate[1 : candidate.index("]")]
    elif candidate.count(":") == 1:
        host, port = candidate.rsplit(":", 1)
        if port.isdigit():
            candidate = host
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        return None


def _trusted_networks(cidr_values: list[str]) -> tuple[ipaddress._BaseNetwork, ...]:
    return tuple(ipaddress.ip_network(value, strict=False) for value in cidr_values)


def _is_trusted(address: str, networks: tuple[ipaddress._BaseNetwork, ...]) -> bool:
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return False
    return any(parsed in network for network in networks)


def _header(headers: Mapping[bytes, bytes], name: str, max_length: int) -> str | None:
    value = headers.get(name.encode("ascii"))
    if value is None or len(value) > max_length:
        return None
    return value.decode("ascii", errors="ignore")


def _forwarded_addresses(value: str, max_hops: int) -> list[str]:
    addresses: list[str] = []
    for match in _FORWARDED_FOR_RE.finditer(value):
        parsed = _parse_ip(match.group(1) or match.group(2) or "")
        if parsed is None:
            return []
        addresses.append(parsed)
    if not addresses or len(addresses) > max_hops:
        return []
    return addresses


def resolve_client_ip(peer: str | None, headers: Mapping[bytes, bytes], settings) -> str:
    """Resolve the first untrusted address from a trusted proxy chain.

    Forwarding headers are considered only when the immediate ASGI peer is in
    the configured proxy networks. Once trusted, the chain is scanned from
    right to left and the first address outside those networks is selected.
    This prevents a direct client from choosing its own rate-limit identity.
    """
    peer_address = _parse_ip(peer or "") or "unknown"
    if not settings.trust_forwarded_headers:
        return peer_address
    networks = _trusted_networks(settings.trusted_proxy_cidrs)
    if not _is_trusted(peer_address, networks):
        return peer_address

    max_length = settings.forwarded_header_max_length
    max_hops = settings.forwarded_max_hops
    forwarded_for = _header(headers, "x-forwarded-for", max_length)
    addresses = []
    if forwarded_for:
        values = [item.strip() for item in forwarded_for.split(",")]
        if len(values) <= max_hops:
            addresses = [_parse_ip(item) for item in values]
            if any(item is None for item in addresses):
                addresses = []
            else:
                addresses = [item for item in addresses if item is not None]
    if not addresses:
        forwarded = _header(headers, "forwarded", max_length)
        if forwarded:
            addresses = _forwarded_addresses(forwarded, max_hops)
    if not addresses:
        cloudflare = _header(headers, "cf-connecting-ip", max_length)
        parsed = _parse_ip(cloudflare or "") if cloudflare else None
        if parsed:
            addresses = [parsed]

    if not addresses:
        return peer_address
    for address in reversed(addresses):
        if not _is_trusted(address, networks):
            return address
    return addresses[0]


def resolve_forwarded_scheme(peer: str | None, headers: Mapping[bytes, bytes], settings) -> str | None:
    """Return a trusted forwarded scheme, or ``None`` when unavailable."""
    if not settings.trust_forwarded_headers:
        return None
    peer_address = _parse_ip(peer or "") or "unknown"
    networks = _trusted_networks(settings.trusted_proxy_cidrs)
    if not _is_trusted(peer_address, networks):
        return None
    value = _header(headers, "x-forwarded-proto", settings.forwarded_header_max_length)
    if value:
        scheme = value.split(",", 1)[0].strip().lower()
        if scheme in {"http", "https"}:
            return scheme
    forwarded = _header(headers, "forwarded", settings.forwarded_header_max_length)
    if forwarded:
        for item in forwarded.split(",", 1)[0].split(";"):
            key, _, value = item.partition("=")
            if key.strip().lower() == "proto" and value.strip().lower() in {"http", "https"}:
                return value.strip().lower()
    return None
