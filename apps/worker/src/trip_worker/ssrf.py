"""SSRF guard and IP-pinned HTTP transport for headless Chromium Playwright requests."""

from __future__ import annotations

import asyncio
import contextvars
import ipaddress
import posixpath
import socket
import ssl
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import ParseResult, unquote, urlparse

import httpcore
import httpx

RenderRequestKind = Literal["data_or_blob", "render_origin", "external_https", "blocked"]


@dataclass(frozen=True, slots=True)
class ResolvedExternalTarget:
    """Validated external HTTPS target with optional DNS-pinned IP address."""

    url: str
    host: str
    port: int
    resolved_ip: str | None = None


@dataclass(frozen=True, slots=True)
class RenderRequestDecision:
    """Result of SSRF policy validation and optional DNS resolution."""

    kind: RenderRequestKind
    target: ResolvedExternalTarget | None = None

    @property
    def allowed(self) -> bool:
        return self.kind != "blocked"


class SsrfBlockedConnectError(httpcore.ConnectError):
    """Raised when an outbound TCP connection violates SSRF IP restrictions."""


_PINNED_TARGET_CTX: contextvars.ContextVar[ResolvedExternalTarget | None] = contextvars.ContextVar(
    "trip_worker_ssrf_pinned_target", default=None
)


@contextmanager
def pin_external_target(target: ResolvedExternalTarget | None) -> Iterator[None]:
    """Bind a validated ResolvedExternalTarget to the active async context for IP pinning."""
    token = _PINNED_TARGET_CTX.set(target)
    try:
        yield
    finally:
        _PINNED_TARGET_CTX.reset(token)


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


def _is_ip_literal(host: str) -> bool:
    clean = host.strip().strip("[]").split("%", 1)[0]
    if not clean:
        return False
    try:
        ipaddress.ip_address(clean)
        return True
    except ValueError:
        return False


def _normalize_ip_literal(ip_str: str) -> str:
    clean_ip = ip_str.split("%", 1)[0]
    return ipaddress.ip_address(clean_ip).compressed.lower()


def _normalize_host(host: str) -> str:
    stripped = host.strip().strip("[]").lower()
    if not stripped:
        return ""
    try:
        return ipaddress.ip_address(stripped.split("%", 1)[0]).compressed.lower()
    except ValueError:
        return stripped


def _normalize_allowed_hosts(allowed_external_hosts: frozenset[str]) -> frozenset[str]:
    return frozenset(norm for h in allowed_external_hosts if (norm := _normalize_host(h)))


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


def _classify_render_request(
    request_url: str,
    *,
    render_origin: str,
    allowed_external_hosts: frozenset[str],
) -> tuple[RenderRequestKind, str | None, int]:
    """Classify request URL and validate origin/allowlist synchronously."""
    parsed = urlparse(request_url)
    if parsed.scheme in {"data", "blob"}:
        return ("data_or_blob", None, 0)

    if parsed.username or parsed.password:
        return ("blocked", None, 0)

    req_origin = _normalized_origin(parsed)
    base_origin = _normalized_origin(urlparse(render_origin))
    if req_origin is not None and base_origin is not None and req_origin == base_origin:
        if _is_disallowed_origin_path(parsed.path):
            return ("blocked", None, 0)
        return ("render_origin", None, 0)

    if parsed.scheme.lower() != "https":
        return ("blocked", None, 0)

    host = _normalize_host(parsed.hostname or "")
    if not host or host not in _normalize_allowed_hosts(allowed_external_hosts):
        return ("blocked", None, 0)

    try:
        port = parsed.port or 443
    except ValueError:
        return ("blocked", None, 0)

    try:
        if _is_restricted_ip(host):
            return ("blocked", None, 0)
    except ValueError:
        pass

    return ("external_https", host, port)


def _precheck_render_request(
    request_url: str,
    *,
    render_origin: str,
    allowed_external_hosts: frozenset[str],
) -> tuple[bool, str | None, int]:
    """Run synchronous URL, origin, and allowlist checks; return DNS lookup target if needed."""
    kind, host, port = _classify_render_request(
        request_url,
        render_origin=render_origin,
        allowed_external_hosts=allowed_external_hosts,
    )
    return (kind != "blocked", host, port)


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
    if not infos:
        return False
    for info in infos:
        try:
            if _is_restricted_ip(str(info[4][0])):
                return False
        except ValueError:
            return False

    return True


async def resolve_and_validate_external_target_async(
    request_url: str,
    *,
    render_origin: str,
    allowed_external_hosts: frozenset[str],
    resolve_dns: bool = True,
) -> RenderRequestDecision:
    """Validate request against SSRF policy and resolve/pin public IP for external HTTPS."""
    kind, host, port = _classify_render_request(
        request_url,
        render_origin=render_origin,
        allowed_external_hosts=allowed_external_hosts,
    )
    if kind == "blocked":
        return RenderRequestDecision(kind="blocked")
    if kind != "external_https" or host is None:
        return RenderRequestDecision(kind=kind)

    if not resolve_dns:
        resolved_ip = _normalize_ip_literal(host) if _is_ip_literal(host) else None
        return RenderRequestDecision(
            kind="external_https",
            target=ResolvedExternalTarget(
                url=request_url,
                host=host,
                port=port,
                resolved_ip=resolved_ip,
            ),
        )

    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        return RenderRequestDecision(kind="blocked")
    if not infos:
        return RenderRequestDecision(kind="blocked")

    validated_ips: list[str] = []
    for info in infos:
        raw_ip = str(info[4][0])
        try:
            if _is_restricted_ip(raw_ip):
                return RenderRequestDecision(kind="blocked")
            validated_ips.append(_normalize_ip_literal(raw_ip))
        except ValueError:
            return RenderRequestDecision(kind="blocked")

    return RenderRequestDecision(
        kind="external_https",
        target=ResolvedExternalTarget(
            url=request_url,
            host=host,
            port=port,
            resolved_ip=validated_ips[0],
        ),
    )


async def is_render_request_allowed_async(
    request_url: str,
    *,
    render_origin: str,
    allowed_external_hosts: frozenset[str],
    resolve_dns: bool = True,
) -> bool:
    """Async SSRF guard using non-blocking `loop.getaddrinfo` DNS resolution."""
    decision = await resolve_and_validate_external_target_async(
        request_url,
        render_origin=render_origin,
        allowed_external_hosts=allowed_external_hosts,
        resolve_dns=resolve_dns,
    )
    return decision.allowed


def _extract_peer_ip(stream: httpcore.AsyncNetworkStream) -> str | None:
    sock = stream.get_extra_info("socket")
    if sock is not None and hasattr(sock, "getpeername"):
        try:
            peer = sock.getpeername()
        except OSError:
            peer = None
        if isinstance(peer, tuple) and peer:
            return str(peer[0])
    server_addr = stream.get_extra_info("server_addr")
    if isinstance(server_addr, tuple) and server_addr:
        return str(server_addr[0])
    return None


class SsrfPinnedNetworkBackend(httpcore.AsyncNetworkBackend):
    """httpcore AsyncNetworkBackend that connects to pinned public IPs and verifies peer IPs."""

    def __init__(
        self,
        *,
        inner: httpcore.AsyncNetworkBackend | None = None,
        resolve_dns: bool = True,
        require_verified_peer: bool = True,
    ) -> None:
        self._inner: httpcore.AsyncNetworkBackend = inner or httpcore.AnyIOBackend()
        self._resolve_dns = resolve_dns
        self._require_verified_peer = require_verified_peer

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,  # noqa: ASYNC109 - matches httpcore interface
        local_address: str | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        norm_host = _normalize_host(host)
        if not norm_host:
            raise SsrfBlockedConnectError("Empty target host")

        if _is_ip_literal(norm_host):
            if _is_restricted_ip(norm_host):
                raise SsrfBlockedConnectError(f"Blocked restricted target IP {norm_host}")
            connect_host = norm_host
        else:
            pinned = _PINNED_TARGET_CTX.get()
            if (
                pinned is not None
                and _normalize_host(pinned.host) == norm_host
                and pinned.port == port
                and pinned.resolved_ip is not None
            ):
                if _is_restricted_ip(pinned.resolved_ip):
                    raise SsrfBlockedConnectError(
                        f"Blocked restricted pinned IP {pinned.resolved_ip}"
                    )
                connect_host = pinned.resolved_ip
            elif not self._resolve_dns:
                connect_host = host
            else:
                loop = asyncio.get_running_loop()
                try:
                    infos = await loop.getaddrinfo(norm_host, port, type=socket.SOCK_STREAM)
                except OSError as exc:
                    raise SsrfBlockedConnectError(f"DNS resolution failed for {norm_host}") from exc
                if not infos:
                    raise SsrfBlockedConnectError(f"DNS returned no addresses for {norm_host}")
                validated_ips: list[str] = []
                for info in infos:
                    raw_ip = str(info[4][0])
                    try:
                        if _is_restricted_ip(raw_ip):
                            raise SsrfBlockedConnectError(
                                f"Blocked restricted resolved IP {raw_ip} for {norm_host}"
                            )
                        validated_ips.append(_normalize_ip_literal(raw_ip))
                    except ValueError as exc:
                        raise SsrfBlockedConnectError(
                            f"Invalid resolved IP {raw_ip} for {norm_host}"
                        ) from exc
                connect_host = validated_ips[0]

        stream = await self._inner.connect_tcp(
            connect_host,
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )

        peer_ip = _extract_peer_ip(stream)
        if peer_ip is None:
            if self._require_verified_peer:
                await stream.aclose()
                raise SsrfBlockedConnectError("Unable to verify connected socket peer IP")
        else:
            try:
                is_bad_peer = _is_restricted_ip(peer_ip)
            except ValueError:
                is_bad_peer = True
            if is_bad_peer:
                await stream.aclose()
                raise SsrfBlockedConnectError(f"Blocked restricted connected peer IP {peer_ip}")

        return stream

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,  # noqa: ASYNC109 - matches httpcore interface
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        _ = path, timeout, socket_options
        raise SsrfBlockedConnectError("Unix domain sockets are disabled for external requests")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


class PinnedSsrfAsyncTransport(httpx.AsyncHTTPTransport):
    """httpx AsyncHTTPTransport enforcing IP-pinned TCP connections and peer IP checks."""

    def __init__(
        self,
        *,
        resolve_dns: bool = True,
        require_verified_peer: bool = True,
        network_backend: httpcore.AsyncNetworkBackend | None = None,
        verify: ssl.SSLContext | str | bool = True,
        limits: httpx.Limits | None = None,
    ) -> None:
        resolved_limits = limits or httpx.Limits(max_connections=32, max_keepalive_connections=16)
        super().__init__(
            verify=verify,
            trust_env=False,
            http1=True,
            http2=False,
            limits=resolved_limits,
            retries=0,
        )
        ssl_context = httpx.create_ssl_context(verify=verify, trust_env=False)
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=ssl_context,
            max_connections=resolved_limits.max_connections,
            max_keepalive_connections=resolved_limits.max_keepalive_connections,
            keepalive_expiry=resolved_limits.keepalive_expiry,
            http1=True,
            http2=False,
            retries=0,
            network_backend=SsrfPinnedNetworkBackend(
                inner=network_backend,
                resolve_dns=resolve_dns,
                require_verified_peer=require_verified_peer,
            ),
        )


def build_pinned_external_http_client(
    *,
    resolve_dns: bool = True,
    timeout: float = 15.0,
    transport: httpx.AsyncBaseTransport | None = None,
) -> httpx.AsyncClient:
    """Create an httpx.AsyncClient pinned to SSRF-validated IPs with redirects and env disabled."""
    resolved_transport = transport or PinnedSsrfAsyncTransport(resolve_dns=resolve_dns)
    return httpx.AsyncClient(
        transport=resolved_transport,
        follow_redirects=False,
        trust_env=False,
        timeout=timeout,
    )


def build_host_header(target: ResolvedExternalTarget) -> str:
    """Format an HTTP Host header value matching the original target host and port."""
    formatted_host = f"[{target.host}]" if ":" in target.host else target.host
    if target.port == 443:
        return formatted_host
    return f"{formatted_host}:{target.port}"


def build_external_request_kwargs(target: ResolvedExternalTarget) -> dict[str, Any]:
    """Return explicit Host header and SNI extension kwargs for pinned external fetches."""
    return {
        "headers": {"Host": build_host_header(target)},
        "extensions": {"sni_hostname": target.host},
    }
