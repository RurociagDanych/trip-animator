from __future__ import annotations

import base64
import hmac
import json
import logging
import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from time import monotonic
from typing import Any, cast
from urllib.parse import urlencode

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from fastapi import HTTPException, Request

from trip_api.config import Settings
from trip_api.security import (
    SsrfBlockedError,
    decrypt_secret,
    encrypt_secret,
    validate_outbound_url,
)

logger = logging.getLogger(__name__)

GOOGLE_OIDC_COOKIE_NAME = "trip_google_oidc"
GOOGLE_OIDC_COOKIE_PATH = "/api/v1/auth/google"
GOOGLE_OIDC_STATE_TTL_SECONDS = 600

_GOOGLE_OIDC_STATE_AAD = b"google-oidc-state-v1"
_GOOGLE_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_GOOGLE_OAUTH_ENDPOINT = "https://oauth2.googleapis.com/token"
_GOOGLE_JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"
_GOOGLE_ISSUERS = frozenset({"https://accounts.google.com", "accounts.google.com"})
_JWKS_CACHE_TTL_SECONDS = 3600.0


@dataclass(frozen=True, slots=True)
class GoogleOidcState:
    state: str
    nonce: str
    code_verifier: str
    expires_at: int


@dataclass(frozen=True, slots=True)
class GoogleIdentityClaims:
    subject: str
    email: str
    display_name: str


def _b64url_decode(segment: str) -> bytes:
    padded = segment + "=" * ((4 - len(segment) % 4) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii"))


def _b64url_to_int(value: str) -> int:
    return int.from_bytes(_b64url_decode(value), "big")


def encode_oidc_state_cookie(payload: GoogleOidcState, settings: Settings) -> str:
    raw = json.dumps(
        {
            "state": payload.state,
            "nonce": payload.nonce,
            "code_verifier": payload.code_verifier,
            "expires_at": payload.expires_at,
        },
        separators=(",", ":"),
    )
    return encrypt_secret(raw, settings.decoded_encryption_key, aad=_GOOGLE_OIDC_STATE_AAD)


def decode_oidc_state_cookie(raw_cookie: str | None, settings: Settings) -> GoogleOidcState | None:
    if not raw_cookie:
        return None
    try:
        decrypted = decrypt_secret(
            raw_cookie,
            settings.decoded_encryption_key,
            aad=_GOOGLE_OIDC_STATE_AAD,
        )
        data = json.loads(decrypted)
    except Exception:
        return None

    if not isinstance(data, dict):
        return None
    state = data.get("state")
    nonce = data.get("nonce")
    code_verifier = data.get("code_verifier")
    expires_at = data.get("expires_at")
    if (
        not isinstance(state, str)
        or not isinstance(nonce, str)
        or not isinstance(code_verifier, str)
        or not isinstance(expires_at, int)
    ):
        return None

    if expires_at <= int(datetime.now(UTC).timestamp()):
        return None

    return GoogleOidcState(
        state=state,
        nonce=nonce,
        code_verifier=code_verifier,
        expires_at=expires_at,
    )


def build_google_authorize_url(
    *,
    settings: Settings,
    state: str,
    nonce: str,
    code_challenge: str,
) -> str:
    query = urlencode(
        {
            "client_id": settings.google_auth_client_id.strip(),
            "redirect_uri": settings.google_auth_redirect_uri.strip(),
            "response_type": "code",
            "scope": "openid email profile",
            "state": state,
            "nonce": nonce,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "prompt": "select_account",
        }
    )
    return f"{_GOOGLE_AUTHORIZE_URL}?{query}"


def _get_http_client_factory(request: Request) -> Callable[[], httpx.AsyncClient] | None:
    factory = getattr(request.app.state, "google_http_client_factory", None)
    if factory is None:
        return None
    return cast(Callable[[], httpx.AsyncClient], factory)


async def _fetch_jwks(
    request: Request,
    settings: Settings,
    *,
    force_refresh: bool = False,
) -> dict[str, rsa.RSAPublicKey]:
    cached = getattr(request.app.state, "google_jwks_cache", None)
    now_mono = monotonic()
    if not force_refresh and isinstance(cached, tuple) and len(cached) == 2:
        cached_at, cached_keys = cached
        if isinstance(cached_at, float) and (now_mono - cached_at) < _JWKS_CACHE_TTL_SECONDS:
            return cast(dict[str, rsa.RSAPublicKey], cached_keys)

    custom_factory = _get_http_client_factory(request)
    try:
        validate_outbound_url(
            _GOOGLE_JWKS_URL,
            allowed_hosts=settings.google_auth_allowed_hosts,
            resolve_dns=(custom_factory is None),
        )
    except SsrfBlockedError as exc:
        logger.warning("Blocked outbound Google JWKS URL: %s", type(exc).__name__)
        raise HTTPException(
            status_code=502, detail="Google authentication service is unavailable"
        ) from exc

    client_factory = custom_factory or (
        lambda: httpx.AsyncClient(timeout=10.0, follow_redirects=False)
    )
    try:
        async with client_factory() as client:
            resp = await client.get(_GOOGLE_JWKS_URL, follow_redirects=False)
    except httpx.HTTPError as exc:
        logger.warning("Google JWKS fetch failed: %s", type(exc).__name__)
        raise HTTPException(
            status_code=502, detail="Google authentication service is unavailable"
        ) from exc

    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail="Google authentication service is unavailable")

    try:
        payload: Any = resp.json()
    except ValueError as exc:
        raise HTTPException(status_code=502, detail="Invalid Google signing key response") from exc

    raw_keys = payload.get("keys") if isinstance(payload, dict) else None
    if not isinstance(raw_keys, list):
        raise HTTPException(status_code=502, detail="Invalid Google signing key response")

    parsed_keys: dict[str, rsa.RSAPublicKey] = {}
    for entry in raw_keys:
        if not isinstance(entry, dict) or entry.get("kty") != "RSA":
            continue
        kid = entry.get("kid")
        n_val = entry.get("n")
        e_val = entry.get("e")
        if not isinstance(kid, str) or not isinstance(n_val, str) or not isinstance(e_val, str):
            continue
        try:
            pub_numbers = rsa.RSAPublicNumbers(_b64url_to_int(e_val), _b64url_to_int(n_val))
            parsed_keys[kid] = pub_numbers.public_key()
        except Exception as exc:
            logger.debug("Skipping malformed Google JWKS key: %s", type(exc).__name__)
            continue

    request.app.state.google_jwks_cache = (now_mono, parsed_keys)
    return parsed_keys


async def _verify_id_token(
    *,
    id_token: str,
    expected_nonce: str,
    request: Request,
    settings: Settings,
) -> GoogleIdentityClaims:
    parts = id_token.split(".")
    if len(parts) != 3:
        raise HTTPException(status_code=400, detail="Invalid Google ID token")

    header_b64, payload_b64, sig_b64 = parts
    try:
        header = json.loads(_b64url_decode(header_b64))
        claims = json.loads(_b64url_decode(payload_b64))
        signature = _b64url_decode(sig_b64)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid Google ID token") from exc

    if not isinstance(header, dict) or not isinstance(claims, dict):
        raise HTTPException(status_code=400, detail="Invalid Google ID token")

    if header.get("alg") != "RS256":
        raise HTTPException(status_code=400, detail="Unsupported Google ID token algorithm")

    kid = header.get("kid")
    if not isinstance(kid, str) or not kid:
        raise HTTPException(status_code=400, detail="Google ID token missing key identifier")

    keys = await _fetch_jwks(request, settings, force_refresh=False)
    public_key = keys.get(kid)
    if public_key is None:
        keys = await _fetch_jwks(request, settings, force_refresh=True)
        public_key = keys.get(kid)
    if public_key is None:
        raise HTTPException(status_code=400, detail="Unknown Google signing key")

    signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
    try:
        public_key.verify(signature, signing_input, padding.PKCS1v15(), hashes.SHA256())
    except InvalidSignature as exc:
        raise HTTPException(status_code=400, detail="Invalid Google ID token signature") from exc

    iss = claims.get("iss")
    aud = claims.get("aud")
    exp = claims.get("exp")
    iat = claims.get("iat")
    nonce = claims.get("nonce")
    sub = claims.get("sub")
    email_raw = claims.get("email")
    email_verified = claims.get("email_verified")

    now_ts = int(datetime.now(UTC).timestamp())
    if iss not in _GOOGLE_ISSUERS:
        raise HTTPException(status_code=400, detail="Invalid Google ID token issuer")
    if not isinstance(aud, str) or not hmac.compare_digest(
        aud.encode("utf-8"), settings.google_auth_client_id.strip().encode("utf-8")
    ):
        raise HTTPException(status_code=400, detail="Invalid Google ID token audience")
    if (
        isinstance(exp, bool)
        or not isinstance(exp, int | float)
        or not math.isfinite(exp)
        or exp <= now_ts - 30
    ):
        raise HTTPException(status_code=400, detail="Expired Google ID token")
    if iat is not None and (
        isinstance(iat, bool)
        or not isinstance(iat, int | float)
        or not math.isfinite(iat)
        or iat > now_ts + 300
    ):
        raise HTTPException(status_code=400, detail="Invalid Google ID token issue time")
    if not isinstance(nonce, str) or not hmac.compare_digest(
        nonce.encode("utf-8"), expected_nonce.encode("utf-8")
    ):
        raise HTTPException(status_code=400, detail="Invalid Google OIDC nonce")
    if not isinstance(sub, str) or not sub.strip() or len(sub.strip()) > 255:
        raise HTTPException(status_code=400, detail="Invalid Google subject identifier")
    if email_verified is not True:
        raise HTTPException(
            status_code=400,
            detail="Google account email address must be verified",
        )
    if not isinstance(email_raw, str):
        raise HTTPException(status_code=400, detail="Google account email is missing")
    email = email_raw.strip().lower()
    if "@" not in email or email.startswith("@") or email.endswith("@") or len(email) > 320:
        raise HTTPException(status_code=400, detail="Google account email is invalid")

    raw_name = claims.get("name")
    fallback_name = email.split("@", 1)[0]
    display_name = (
        raw_name.strip() if isinstance(raw_name, str) and raw_name.strip() else fallback_name
    )[:120]
    return GoogleIdentityClaims(subject=sub.strip(), email=email, display_name=display_name)


async def exchange_google_code(
    *,
    request: Request,
    settings: Settings,
    code: str,
    code_verifier: str,
    expected_nonce: str,
) -> GoogleIdentityClaims:
    custom_factory = _get_http_client_factory(request)
    try:
        validate_outbound_url(
            _GOOGLE_OAUTH_ENDPOINT,
            allowed_hosts=settings.google_auth_allowed_hosts,
            resolve_dns=(custom_factory is None),
        )
    except SsrfBlockedError as exc:
        logger.warning("Blocked outbound Google token URL: %s", type(exc).__name__)
        raise HTTPException(
            status_code=502, detail="Google authentication service is unavailable"
        ) from exc

    client_factory = custom_factory or (
        lambda: httpx.AsyncClient(timeout=10.0, follow_redirects=False)
    )
    try:
        async with client_factory() as client:
            resp = await client.post(
                _GOOGLE_OAUTH_ENDPOINT,
                data={
                    "code": code,
                    "client_id": settings.google_auth_client_id.strip(),
                    "client_secret": settings.google_auth_client_secret.strip(),
                    "redirect_uri": settings.google_auth_redirect_uri.strip(),
                    "grant_type": "authorization_code",
                    "code_verifier": code_verifier,
                },
                follow_redirects=False,
            )
    except httpx.HTTPError as exc:
        logger.warning("Google token exchange request failed: %s", type(exc).__name__)
        raise HTTPException(
            status_code=502, detail="Google authentication service is unavailable"
        ) from exc

    if resp.status_code != 200:
        raise HTTPException(status_code=400, detail="Google authorization code exchange failed")

    try:
        token_payload: Any = resp.json()
    except ValueError as exc:
        raise HTTPException(status_code=502, detail="Invalid Google token response") from exc

    id_token = token_payload.get("id_token") if isinstance(token_payload, dict) else None
    if not isinstance(id_token, str) or not id_token.strip():
        raise HTTPException(status_code=400, detail="Google response missing ID token")

    return await _verify_id_token(
        id_token=id_token.strip(),
        expected_nonce=expected_nonce,
        request=request,
        settings=settings,
    )
