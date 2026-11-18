"""Authentication, invite-code registration, session cookie, CSRF, and dev-login endpoints."""

from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from trip_api.config import Settings
from trip_api.db import AuditEvent, SessionRecord, User
from trip_api.deps import (
    AppSettings,
    CsrfAuth,
    CurrentAuth,
    DbSession,
    get_client_ip,
)
from trip_api.rate_limit import check_rate_limit
from trip_api.schemas import LoginRequest, RegisterRequest, SessionInfoOut
from trip_api.security import (
    generate_opaque_token,
    hash_password,
    hash_session_token,
    verify_password,
)
from trip_api.services import user_to_out

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])

# Precomputed valid Argon2id hash used for constant-time fallback when user is unknown.
_DUMMY_ARGON2_HASH = hash_password("dummy-timing-guard-password-never-used")


def _verify_invite_code(provided_code: str, valid_codes: tuple[str, ...]) -> bool:
    """Compare an invite code against all configured codes in constant time."""
    provided_bytes = provided_code.strip().encode("utf-8")
    matched = False
    for valid_code in valid_codes:
        if hmac.compare_digest(provided_bytes, valid_code.encode("utf-8")):
            matched = True
    return matched


def _issue_session(
    *,
    db: Session,
    response: Response,
    settings: Settings,
    user: User,
) -> SessionInfoOut:
    raw_token = generate_opaque_token(32)
    csrf_token = generate_opaque_token(32)
    expires_at = datetime.now(UTC) + timedelta(hours=settings.session_ttl_hours)

    session_row = SessionRecord(
        user_id=user.id,
        token_hash=hash_session_token(raw_token, settings.session_secret),
        csrf_token=csrf_token,
        expires_at=expires_at,
    )
    db.add(session_row)
    db.commit()

    response.set_cookie(
        key=settings.session_cookie_name,
        value=raw_token,
        httponly=True,
        samesite="lax",
        secure=settings.app_env == "production",
        path="/",
        max_age=settings.session_ttl_hours * 3600,
    )
    response.headers["X-CSRF-Token"] = csrf_token
    return SessionInfoOut(
        user=user_to_out(db, user),
        csrf_token=csrf_token,
        dev_auth_enabled=settings.dev_auth_enabled and settings.app_env != "production",
    )


@router.post("/login", response_model=SessionInfoOut)
def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    db: DbSession,
    settings: AppSettings,
) -> SessionInfoOut:
    client_ip = get_client_ip(request)
    email = body.email.strip().lower()
    email_hash = hashlib.sha256(email.encode("utf-8")).hexdigest()[:32]

    check_rate_limit(
        db,
        bucket_key=f"login:ip:{client_ip}",
        limit=settings.rate_limit_login_max,
        window_seconds=settings.rate_limit_login_window_s,
    )
    check_rate_limit(
        db,
        bucket_key=f"login:acct:{email_hash}",
        limit=settings.rate_limit_login_account_max,
        window_seconds=settings.rate_limit_login_window_s,
    )

    user = db.scalar(select(User).where(User.email == email))
    if user is None:
        verify_password(_DUMMY_ARGON2_HASH, body.password)
        raise HTTPException(status_code=401, detail="Invalid email or password")

    if not verify_password(user.password_hash, body.password) or not user.is_active:
        raise HTTPException(status_code=401, detail="Invalid email or password")

    db.add(AuditEvent(user_id=user.id, event_type="auth.login", metadata_json={}))
    return _issue_session(db=db, response=response, settings=settings, user=user)


@router.post("/register", response_model=SessionInfoOut, status_code=201)
def register(
    body: RegisterRequest,
    request: Request,
    response: Response,
    db: DbSession,
    settings: AppSettings,
) -> SessionInfoOut:
    client_ip = get_client_ip(request)
    email = body.email.strip().lower()
    email_hash = hashlib.sha256(email.encode("utf-8")).hexdigest()[:32]

    check_rate_limit(
        db,
        bucket_key=f"register:ip:{client_ip}",
        limit=settings.rate_limit_register_max,
        window_seconds=settings.rate_limit_register_window_s,
    )
    check_rate_limit(
        db,
        bucket_key=f"register:acct:{email_hash}",
        limit=settings.rate_limit_register_account_max,
        window_seconds=settings.rate_limit_register_window_s,
    )

    if not settings.registration_enabled:
        raise HTTPException(status_code=403, detail="Account registration is disabled")

    if not _verify_invite_code(body.invite_code, settings.registration_invite_codes):
        raise HTTPException(status_code=403, detail="Invalid registration invite code")

    if "@" not in email or email.startswith("@") or email.endswith("@"):
        raise HTTPException(status_code=422, detail="Invalid email address")

    existing = db.scalar(select(User).where(User.email == email))
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail="An account with this email already exists",
        )

    user = User(
        email=email,
        display_name=body.display_name.strip()[:120],
        password_hash=hash_password(body.password),
        is_active=True,
    )
    db.add(user)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="An account with this email already exists",
        ) from exc

    db.add(AuditEvent(user_id=user.id, event_type="auth.register", metadata_json={}))
    return _issue_session(db=db, response=response, settings=settings, user=user)


@router.post("/dev-login", response_model=SessionInfoOut)
def dev_login(
    response: Response,
    db: DbSession,
    settings: AppSettings,
) -> SessionInfoOut:
    if settings.app_env == "production" or not settings.dev_auth_enabled:
        raise HTTPException(status_code=403, detail="Development authentication is disabled")

    dev_email = "dev@trip-animator.local"
    user = db.scalar(select(User).where(User.email == dev_email))
    if user is None:
        user = User(
            email=dev_email,
            display_name="Developer",
            password_hash=hash_password(generate_opaque_token(24)),
            is_active=True,
        )
        db.add(user)
        try:
            db.commit()
            db.refresh(user)
        except IntegrityError:
            db.rollback()
            user = db.scalar(select(User).where(User.email == dev_email))
            if user is None:
                raise

    db.add(AuditEvent(user_id=user.id, event_type="auth.dev_login", metadata_json={}))
    return _issue_session(db=db, response=response, settings=settings, user=user)


@router.post("/logout")
def logout(
    response: Response,
    auth: CsrfAuth,
    db: DbSession,
    settings: AppSettings,
) -> dict[str, bool]:
    db.add(AuditEvent(user_id=auth.user.id, event_type="auth.logout", metadata_json={}))
    db.delete(auth.session)
    db.commit()
    response.delete_cookie(key=settings.session_cookie_name, path="/")
    return {"ok": True}


@router.get("/me", response_model=SessionInfoOut)
def me(
    response: Response,
    auth: CurrentAuth,
    db: DbSession,
    settings: AppSettings,
) -> SessionInfoOut:
    response.headers["X-CSRF-Token"] = auth.session.csrf_token
    return SessionInfoOut(
        user=user_to_out(db, auth.user),
        csrf_token=auth.session.csrf_token,
        dev_auth_enabled=settings.dev_auth_enabled and settings.app_env != "production",
    )
