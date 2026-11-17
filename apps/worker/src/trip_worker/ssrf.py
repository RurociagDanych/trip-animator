"""SSRF guard for headless Chromium Playwright requests."""

from __future__ import annotations

import asyncio
import ipaddress
import posixpath
import socket
from urllib.parse import ParseResult, unquote, urlparse


def _is_restricted_ip(ip_str: str) -> bool:
    clean_ip = ip_str.split("%", 1)[0]
    addr = ipaddress.ip_address(clean_ip)
    return (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
    )


def _normalize_host(host: str) -> str:
    stripped = host.strip().strip("[]").lower()
    if not stripped:
        return ""
    try:
        return ipaddress.ip_address(stripped.split("%", 1)[0]).compressed.lower()
    except ValueError:
        return stripped


def _normalize_allowed_hosts(allowed_external_hosts: frozenset[str]) -> frozenset[str]:
    return frozenset(
        norm for h in allowed_external_hosts if (norm := _normalize_host(h))
    )


def _is_disallowed_origin_path(path: str) -> bool:
    decoded = unquote(path or "/")
    collapsed = posixpath.normpath(decoded)
    while collapsed.startswith("//"):
        collapsed = collapsed[1:]
    lowered = collapsed.lower()
    return lowered == "/api" or lowered.startswith("/api/")


def _normalized_origin(parsed: ParseResult) -> tuple[str, str, int] | None:
    scheme = parsed.scheme.lower()
    host = _normalize_host(parsed.hostname or "")
    if scheme not in {"http", "https"} or not host:
        return None
    try:
        port = parsed.port
    except ValueError:
        return None
    if port is None:
        port = 80 if scheme == "http" else 443
    return (scheme, host, port)


def _precheck_render_request(
    request_url: str,
    *,
    render_origin: str,
    allowed_external_hosts: frozenset[str],
) -> tuple[bool, str | None, int]:
    """Run synchronous URL, origin, and allowlist checks; return DNS lookup target if needed."""
    parsed = urlparse(request_url)
    if parsed.scheme in {"data", "blob"}:
        return (True, None, 0)

    if parsed.username or parsed.password:
        return (False, None, 0)

    req_origin = _normalized_origin(parsed)
    base_origin = _normalized_origin(urlparse(render_origin))
    if req_origin is not None and base_origin is not None and req_origin == base_origin:
        if _is_disallowed_origin_path(parsed.path):
            return (False, None, 0)
        return (True, None, 0)

    if parsed.scheme.lower() != "https":
        return (False, None, 0)

    host = _normalize_host(parsed.hostname or "")
    if not host or host not in _normalize_allowed_hosts(allowed_external_hosts):
        return (False, None, 0)

    try:
        port = parsed.port or 443
    except ValueError:
        return (False, None, 0)

    try:
        if _is_restricted_ip(host):
            return (False, None, 0)
    except ValueError:
        pass

    return (True, host, port)


def is_render_request_allowed(
    request_url: str,
    *,
    render_origin: str,
    allowed_external_hosts: frozenset[str],
    resolve_dns: bool = True,
) -> bool:
    """Allow only internal render origin (excluding /api/*), data/blob URLs, and public tiles."""
    allowed, host, port = _precheck_render_request(
        request_url,
        render_origin=render_origin,
        allowed_external_hosts=allowed_external_hosts,
    )
    if not allowed or host is None or not resolve_dns:
        return allowed

    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        return False
    for info in infos:
        if _is_restricted_ip(str(info[4][0])):
            return False

    return True


async def is_render_request_allowed_async(
    request_url: str,
    *,
    render_origin: str,
    allowed_external_hosts: frozenset[str],
    resolve_dns: bool = True,
) -> bool:
    """Async SSRF guard using non-blocking `loop.getaddrinfo` DNS resolution."""
    allowed, host, port = _precheck_render_request(
        request_url,
        render_origin=render_origin,
        allowed_external_hosts=allowed_external_hosts,
    )
    if not allowed or host is None or not resolve_dns:
        return allowed

    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        return False
    for info in infos:
        if _is_restricted_ip(str(info[4][0])):
            return False

    return True
