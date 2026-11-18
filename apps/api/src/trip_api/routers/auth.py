"""Authentication, invite-code registration, session cookie, CSRF, and dev-login endpoints."""

from __future__ import annotations

import hashlib
import hmac
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from fastapi import APIRouter, HTTPException, Request, Response
from sqlalchemy import case, delete, select
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from trip_api.config import Settings
from trip_api.db import (
    AuditEvent,
    EmailVerificationToken,
    PasswordResetToken,
    SessionRecord,
    User,
)
from trip_api.deps import (
    AppSettings,
    CsrfAuth,
    CurrentAuth,
    DbSession,
    ensure_utc,
    get_client_ip,
)
from trip_api.email import (
    build_password_reset_url,
    build_verification_url,
    send_password_reset_email,
    send_verification_email,
)
from trip_api.rate_limit import check_rate_limit
from trip_api.schemas import (
    AuthSessionOut,
    ChangePasswordRequest,
    ChangePasswordResponse,
    ForgotPasswordRequest,
    ForgotPasswordResponse,
    LoginRequest,
    LogoutAllResponse,
    RegisterRequest,
    RegisterResponse,
    ResendVerificationRequest,
    ResendVerificationResponse,
    ResetPasswordRequest,
    ResetPasswordResponse,
    SessionInfoOut,
    VerifyEmailRequest,
)
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


def _hash_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def _issue_verification_token(db: Session, settings: Settings, user: User) -> str:
    """Replace any unused verification tokens for the user and return a fresh raw token."""
    db.execute(
        delete(EmailVerificationToken).where(
            EmailVerificationToken.user_id == user.id,
            EmailVerificationToken.used_at.is_(None),
        )
    )
    raw_token = generate_opaque_token(32)
    expires_at = datetime.now(UTC) + timedelta(hours=settings.email_verification_ttl_hours)
    db.add(
        EmailVerificationToken(
            user_id=user.id,
            token_hash=_hash_token(raw_token),
            expires_at=expires_at,
        )
    )
    return raw_token


def _issue_password_reset_token(db: Session, settings: Settings, user: User) -> str:
    """Replace any unused password reset tokens for the user and return a fresh raw token."""
    db.execute(
        delete(PasswordResetToken).where(
            PasswordResetToken.user_id == user.id,
            PasswordResetToken.used_at.is_(None),
        )
    )
    raw_token = generate_opaque_token(32)
    expires_at = datetime.now(UTC) + timedelta(minutes=settings.password_reset_ttl_minutes)
    db.add(
        PasswordResetToken(
            user_id=user.id,
            token_hash=_hash_token(raw_token),
            expires_at=expires_at,
        )
    )
    return raw_token


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

    if user.email_verified_at is None:
        db.add(AuditEvent(user_id=user.id, event_type="auth.login_unverified", metadata_json={}))
        db.commit()
        raise HTTPException(
            status_code=403,
            detail=(
                "Email address is not verified. "
                "Please check your inbox or request a new verification link."
            ),
        )

    db.add(AuditEvent(user_id=user.id, event_type="auth.login", metadata_json={}))
    return _issue_session(db=db, response=response, settings=settings, user=user)


@router.post("/register", response_model=RegisterResponse, status_code=201)
def register(
    body: RegisterRequest,
    request: Request,
    db: DbSession,
    settings: AppSettings,
) -> RegisterResponse:
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
        email_verified_at=None,
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

    raw_token = _issue_verification_token(db, settings, user)
    db.add(
        AuditEvent(
            user_id=user.id,
            event_type="auth.register",
            metadata_json={"verification_required": True},
        )
    )
    db.commit()

    verify_url = build_verification_url(settings.public_base_url, raw_token)
    send_verification_email(settings, user.email, user.display_name, verify_url)

    return RegisterResponse(
        status="verification_required",
        email=user.email,
        message="Please check your email to verify your account before signing in.",
    )


@router.post("/verify-email", response_model=SessionInfoOut)
def verify_email(
    body: VerifyEmailRequest,
    request: Request,
    response: Response,
    db: DbSession,
    settings: AppSettings,
) -> SessionInfoOut:
    client_ip = get_client_ip(request)
    check_rate_limit(
        db,
        bucket_key=f"verify_email:ip:{client_ip}",
        limit=settings.rate_limit_verify_email_max,
        window_seconds=settings.rate_limit_verify_email_window_s,
    )

    token_hash = _hash_token(body.token.strip())
    token_row = db.scalar(
        select(EmailVerificationToken).where(EmailVerificationToken.token_hash == token_hash)
    )
    now = datetime.now(UTC)
    if (
        token_row is None
        or token_row.used_at is not None
        or ensure_utc(token_row.expires_at) <= now
    ):
        raise HTTPException(status_code=400, detail="Invalid or expired email verification link")

    user = db.get(User, token_row.user_id)
    if user is None or not user.is_active:
        raise HTTPException(status_code=400, detail="Invalid or expired email verification link")

    token_row.used_at = now
    if user.email_verified_at is None:
        user.email_verified_at = now
    db.add(AuditEvent(user_id=user.id, event_type="auth.verify_email", metadata_json={}))
    return _issue_session(db=db, response=response, settings=settings, user=user)


@router.post("/resend-verification", response_model=ResendVerificationResponse, status_code=202)
def resend_verification(
    body: ResendVerificationRequest,
    request: Request,
    db: DbSession,
    settings: AppSettings,
) -> ResendVerificationResponse:
    client_ip = get_client_ip(request)
    email = body.email.strip().lower()
    email_hash = hashlib.sha256(email.encode("utf-8")).hexdigest()[:32]

    check_rate_limit(
        db,
        bucket_key=f"resend:ip:{client_ip}",
        limit=settings.rate_limit_resend_verification_max,
        window_seconds=settings.rate_limit_resend_verification_window_s,
    )
    check_rate_limit(
        db,
        bucket_key=f"resend:acct:{email_hash}",
        limit=settings.rate_limit_resend_verification_account_max,
        window_seconds=settings.rate_limit_resend_verification_window_s,
    )

    generic_msg = (
        "If an unverified account exists for that email address, "
        "a new verification link has been sent."
    )
    user = db.scalar(select(User).where(User.email == email))
    if user is not None and user.is_active and user.email_verified_at is None:
        raw_token = _issue_verification_token(db, settings, user)
        db.add(
            AuditEvent(
                user_id=user.id,
                event_type="auth.resend_verification",
                metadata_json={},
            )
        )
        db.commit()
        verify_url = build_verification_url(settings.public_base_url, raw_token)
        send_verification_email(settings, user.email, user.display_name, verify_url)

    return ResendVerificationResponse(status="accepted", message=generic_msg)


@router.post("/dev-login", response_model=SessionInfoOut)
def dev_login(
    response: Response,
    db: DbSession,
    settings: AppSettings,
) -> SessionInfoOut:
    if settings.app_env == "production" or not settings.dev_auth_enabled:
        raise HTTPException(status_code=403, detail="Development authentication is disabled")

    dev_email = "dev@trip-animator.local"
    now = datetime.now(UTC)
    user = db.scalar(select(User).where(User.email == dev_email))
    if user is None:
        user = User(
            email=dev_email,
            display_name="Developer",
            password_hash=hash_password(generate_opaque_token(24)),
            is_active=True,
            email_verified_at=now,
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
    if user.email_verified_at is None:
        user.email_verified_at = now

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


@router.post("/forgot-password", response_model=ForgotPasswordResponse, status_code=202)
def forgot_password(
    body: ForgotPasswordRequest,
    request: Request,
    db: DbSession,
    settings: AppSettings,
) -> ForgotPasswordResponse:
    client_ip = get_client_ip(request)
    email = body.email.strip().lower()
    email_hash = hashlib.sha256(email.encode("utf-8")).hexdigest()[:32]

    check_rate_limit(
        db,
        bucket_key=f"forgot_password:ip:{client_ip}",
        limit=settings.rate_limit_forgot_password_max,
        window_seconds=settings.rate_limit_forgot_password_window_s,
    )
    check_rate_limit(
        db,
        bucket_key=f"forgot_password:acct:{email_hash}",
        limit=settings.rate_limit_forgot_password_account_max,
        window_seconds=settings.rate_limit_forgot_password_window_s,
    )

    generic_msg = (
        "If an active account exists for that email address, a password reset link has been sent."
    )
    user = db.scalar(select(User).where(User.email == email))
    if user is not None and user.is_active:
        raw_token = _issue_password_reset_token(db, settings, user)
        db.add(AuditEvent(user_id=user.id, event_type="auth.forgot_password", metadata_json={}))
        db.commit()
        reset_url = build_password_reset_url(settings.public_base_url, raw_token)
        send_password_reset_email(settings, user.email, user.display_name, reset_url)

    return ForgotPasswordResponse(status="accepted", message=generic_msg)


@router.post("/reset-password", response_model=ResetPasswordResponse)
def reset_password(
    body: ResetPasswordRequest,
    request: Request,
    db: DbSession,
    settings: AppSettings,
) -> ResetPasswordResponse:
    client_ip = get_client_ip(request)
    check_rate_limit(
        db,
        bucket_key=f"reset_password:ip:{client_ip}",
        limit=settings.rate_limit_reset_password_max,
        window_seconds=settings.rate_limit_reset_password_window_s,
    )

    token_hash = _hash_token(body.token.strip())
    token_row = db.scalar(
        select(PasswordResetToken).where(PasswordResetToken.token_hash == token_hash)
    )
    now = datetime.now(UTC)
    if (
        token_row is None
        or token_row.used_at is not None
        or ensure_utc(token_row.expires_at) <= now
    ):
        raise HTTPException(status_code=400, detail="Invalid or expired password reset link")

    user = db.get(User, token_row.user_id)
    if user is None or not user.is_active:
        raise HTTPException(status_code=400, detail="Invalid or expired password reset link")

    token_row.used_at = now
    user.password_hash = hash_password(body.new_password)
    if user.email_verified_at is None:
        user.email_verified_at = now

    db.execute(delete(SessionRecord).where(SessionRecord.user_id == user.id))
    db.add(AuditEvent(user_id=user.id, event_type="auth.password_reset", metadata_json={}))
    db.commit()
    return ResetPasswordResponse(
        ok=True,
        message="Your password has been reset. You can now sign in with your new password.",
    )


@router.post("/change-password", response_model=ChangePasswordResponse)
def change_password(
    body: ChangePasswordRequest,
    auth: CsrfAuth,
    db: DbSession,
) -> ChangePasswordResponse:
    if not verify_password(auth.user.password_hash, body.current_password):
        raise HTTPException(status_code=400, detail="Current password is incorrect")

    if body.new_password == body.current_password or verify_password(
        auth.user.password_hash, body.new_password
    ):
        raise HTTPException(
            status_code=400,
            detail="New password must be different from your current password",
        )

    now = datetime.now(UTC)
    auth.user.password_hash = hash_password(body.new_password)
    db.execute(
        delete(SessionRecord).where(
            SessionRecord.user_id == auth.user.id,
            SessionRecord.id != auth.session.id,
            SessionRecord.expires_at <= now,
        )
    )
    revoked_res = cast(
        CursorResult[Any],
        db.execute(
            delete(SessionRecord).where(
                SessionRecord.user_id == auth.user.id,
                SessionRecord.id != auth.session.id,
            )
        ),
    )
    revoked_count = max(0, int(revoked_res.rowcount or 0))
    db.execute(
        delete(PasswordResetToken).where(
            PasswordResetToken.user_id == auth.user.id,
            PasswordResetToken.used_at.is_(None),
        )
    )
    db.add(
        AuditEvent(
            user_id=auth.user.id,
            event_type="auth.password_changed",
            metadata_json={"revoked_sessions_count": revoked_count},
        )
    )
    db.commit()
    return ChangePasswordResponse(ok=True, revoked_sessions_count=revoked_count)


@router.get("/sessions", response_model=list[AuthSessionOut])
def list_sessions(
    auth: CurrentAuth,
    db: DbSession,
) -> list[AuthSessionOut]:
    now = datetime.now(UTC)
    rows = db.scalars(
        select(SessionRecord)
        .where(SessionRecord.user_id == auth.user.id, SessionRecord.expires_at > now)
        .order_by(
            case((SessionRecord.id == auth.session.id, 0), else_=1),
            SessionRecord.created_at.desc(),
        )
    ).all()
    return [
        AuthSessionOut(
            id=row.id,
            created_at=ensure_utc(row.created_at),
            expires_at=ensure_utc(row.expires_at),
            is_current=(row.id == auth.session.id),
        )
        for row in rows
    ]


@router.delete("/sessions/{session_id}")
def revoke_session(
    session_id: uuid.UUID,
    response: Response,
    auth: CsrfAuth,
    db: DbSession,
    settings: AppSettings,
) -> dict[str, bool]:
    target = db.scalar(
        select(SessionRecord).where(
            SessionRecord.id == session_id,
            SessionRecord.user_id == auth.user.id,
        )
    )
    if target is None:
        raise HTTPException(status_code=404, detail="Session not found")

    is_current = target.id == auth.session.id
    db.delete(target)
    db.add(
        AuditEvent(
            user_id=auth.user.id,
            event_type="auth.session_revoked",
            metadata_json={"session_id": str(session_id)},
        )
    )
    db.commit()
    if is_current:
        response.delete_cookie(key=settings.session_cookie_name, path="/")
    return {"ok": True}


@router.post("/logout-all", response_model=LogoutAllResponse)
def logout_all(
    response: Response,
    auth: CsrfAuth,
    db: DbSession,
    settings: AppSettings,
) -> LogoutAllResponse:
    revoked_res = cast(
        CursorResult[Any],
        db.execute(delete(SessionRecord).where(SessionRecord.user_id == auth.user.id)),
    )
    revoked_count = max(0, int(revoked_res.rowcount or 0))
    db.add(
        AuditEvent(
            user_id=auth.user.id,
            event_type="auth.sessions_revoked",
            metadata_json={"revoked_sessions_count": revoked_count},
        )
    )
    db.commit()
    response.delete_cookie(key=settings.session_cookie_name, path="/")
    return LogoutAllResponse(ok=True, revoked_sessions_count=revoked_count)


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
