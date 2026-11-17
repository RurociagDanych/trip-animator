"""Cryptographic primitives, PKCE, CSRF verification, and SSRF protection."""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import os
import secrets
import socket
from urllib.parse import urlparse

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_PASSWORD_HASHER = PasswordHasher()


def hash_password(plain_password: str) -> str:
    """Hash a password with Argon2id."""
    if len(plain_password) < 12:
        raise ValueError("Password must be at least 12 characters")
    return _PASSWORD_HASHER.hash(plain_password)


def verify_password(hashed_password: str, plain_password: str) -> bool:
    """Verify an Argon2id password hash safely."""
    try:
        return _PASSWORD_HASHER.verify(hashed_password, plain_password)
    except VerifyMismatchError:
        return False


def generate_opaque_token(nbytes: int = 32) -> str:
    """Generate a URL-safe cryptographically random token."""
    return secrets.token_urlsafe(nbytes)


def hash_session_token(token: str, secret: str) -> str:
    """Keyed SHA-256 digest of a raw session cookie for database storage."""
    return hmac.new(secret.encode("utf-8"), token.encode("utf-8"), hashlib.sha256).hexdigest()


def verify_csrf_token(expected_token: str | None, provided_token: str | None) -> bool:
    """Constant-time comparison for CSRF header tokens."""
    if not expected_token or not provided_token:
        return False
    return hmac.compare_digest(expected_token.encode("utf-8"), provided_token.encode("utf-8"))


def generate_pkce_pair() -> tuple[str, str]:
    """Generate OAuth2 PKCE (code_verifier, S256 code_challenge)."""
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def encrypt_secret(plaintext: str, key: bytes, *, aad: bytes = b"oauth-token-v1") -> str:
    """Encrypt sensitive credentials (e.g., Strava refresh/access tokens) using AES-256-GCM."""
    nonce = os.urandom(12)
    ciphertext = AESGCM(key).encrypt(nonce, plaintext.encode("utf-8"), aad)
    blob = base64.urlsafe_b64encode(nonce + ciphertext).decode("ascii")
    return f"v1:{blob}"


def decrypt_secret(token: str, key: bytes, *, aad: bytes = b"oauth-token-v1") -> str:
    """Decrypt a versioned AES-256-GCM secret payload."""
    if not token.startswith("v1:"):
        raise ValueError("Unsupported encrypted secret version")
    raw = base64.urlsafe_b64decode(token[3:].encode("ascii"))
    if len(raw) <= 12:
        raise ValueError("Malformed encrypted payload")
    nonce, ciphertext = raw[:12], raw[12:]
    plaintext = AESGCM(key).decrypt(nonce, ciphertext, aad)
    return plaintext.decode("utf-8")


class SsrfBlockedError(Exception):
    """Raised when an outbound URL violates the host allowlist or resolves to a restricted IP."""


def _is_forbidden_ip(ip_str: str) -> bool:
    addr = ipaddress.ip_address(ip_str)
    return (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
    )


def validate_outbound_url(
    url: str,
    *,
    allowed_hosts: frozenset[str],
    resolve_dns: bool = True,
) -> str:
    """Validate that an outbound HTTPS URL targets an allowlisted public host (anti-SSRF)."""
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise SsrfBlockedError("Only HTTPS outbound requests are permitted")
    if parsed.username or parsed.password:
        raise SsrfBlockedError("Embedded URL credentials are forbidden")

    host = (parsed.hostname or "").lower()
    if not host or host not in allowed_hosts:
        raise SsrfBlockedError(f"Host {host!r} is not on the outbound allowlist")

    is_ip_literal = False
    try:
        ipaddress.ip_address(host)
        is_ip_literal = True
    except ValueError:
        is_ip_literal = False

    if is_ip_literal and _is_forbidden_ip(host):
        raise SsrfBlockedError(f"Direct IP {host!r} is forbidden")

    if resolve_dns:
        try:
            infos = socket.getaddrinfo(host, parsed.port or 443, type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise SsrfBlockedError(f"DNS resolution failed for {host!r}") from exc
        for info in infos:
            ip_candidate = str(info[4][0])
            if _is_forbidden_ip(ip_candidate):
                raise SsrfBlockedError(
                    f"Host {host!r} resolved to restricted IP {ip_candidate!r}"
                )

    return url
