from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, cast

import httpx
from fastapi import HTTPException, Request

from trip_api.config import Settings
from trip_api.security import SsrfBlockedError, validate_outbound_url

logger = logging.getLogger(__name__)

_TURNSTILE_VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
_TURNSTILE_UNAVAILABLE_DETAIL = "Verification challenge could not be checked right now"


def _turnstile_unavailable_error() -> HTTPException:
    return HTTPException(status_code=503, detail=_TURNSTILE_UNAVAILABLE_DETAIL)


def _get_http_client_factory(request: Request) -> Callable[[], httpx.AsyncClient] | None:
    factory = getattr(request.app.state, "turnstile_http_client_factory", None)
    if factory is None:
        return None
    return cast(Callable[[], httpx.AsyncClient], factory)


async def verify_turnstile_token(
    *,
    settings: Settings,
    request: Request,
    token: str | None,
    remote_ip: str | None,
) -> None:
    if not settings.turnstile_enabled:
        return

    cleaned_token = (token or "").strip()
    if not cleaned_token:
        raise HTTPException(
            status_code=400,
            detail="Verification challenge is required",
        )

    custom_factory = _get_http_client_factory(request)
    try:
        validate_outbound_url(
            _TURNSTILE_VERIFY_URL,
            allowed_hosts=settings.turnstile_allowed_hosts,
            resolve_dns=(custom_factory is None),
        )
    except SsrfBlockedError as exc:
        logger.warning("Blocked outbound Turnstile verification URL: %s", type(exc).__name__)
        raise _turnstile_unavailable_error() from exc

    form_payload: dict[str, str] = {
        "secret": settings.turnstile_secret_key.strip(),
        "response": cleaned_token,
    }
    if remote_ip and remote_ip != "unknown":
        form_payload["remoteip"] = remote_ip

    client_factory = custom_factory or (
        lambda: httpx.AsyncClient(timeout=10.0, follow_redirects=False)
    )
    try:
        async with client_factory() as client:
            resp = await client.post(
                _TURNSTILE_VERIFY_URL,
                data=form_payload,
                follow_redirects=False,
            )
    except httpx.HTTPError as exc:
        logger.warning("Turnstile verification request failed: %s", type(exc).__name__)
        raise _turnstile_unavailable_error() from exc

    if resp.status_code != 200:
        logger.warning("Turnstile verification returned status %s", resp.status_code)
        raise _turnstile_unavailable_error()

    try:
        payload: Any = resp.json()
    except ValueError as exc:
        logger.warning("Turnstile verification returned invalid JSON")
        raise _turnstile_unavailable_error() from exc

    if not isinstance(payload, dict) or payload.get("success") is not True:
        raise HTTPException(
            status_code=400,
            detail="Verification challenge failed. Please try again.",
        )
