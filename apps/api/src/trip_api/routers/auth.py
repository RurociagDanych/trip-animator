from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, cast

import httpx
from fastapi import APIRouter, BackgroundTasks, Body, HTTPException, Request, Response
from sqlalchemy import case, delete, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from trip_api.config import Settings
from trip_api.db import (
    AuditEvent,
    EmailVerificationToken,
    OAuthConnection,
    OAuthState,
    PasswordResetToken,
    Project,
    ProjectPhoto,
    RenderJob,
    RenderJobChunk,
    SessionRecord,
    Stage,
    User,
    UserIdentity,
    VaultActivity,
    VaultActivityTile,
    VaultGear,
    VaultSyncState,
)
from trip_api.deps import (
    AppSettings,
    CsrfAuth,
    CurrentAuth,
    DbSession,
    StoreDep,
    ensure_utc,
    get_client_ip,
)
from trip_api.email import (
    build_password_reset_url,
    build_verification_url,
    send_password_reset_email,
    send_verification_email,
)
from trip_api.google_oidc import (
    GOOGLE_OIDC_COOKIE_NAME,
    GOOGLE_OIDC_COOKIE_PATH,
    GOOGLE_OIDC_STATE_TTL_SECONDS,
    GoogleOidcState,
    build_google_authorize_url,
    decode_oidc_state_cookie,
    encode_oidc_state_cookie,
    exchange_google_code,
)
from trip_api.library_view import LibraryView
from trip_api.rate_limit import check_rate_limit
from trip_api.schemas import (
    AuthSessionOut,
    ChangePasswordRequest,
    ChangePasswordResponse,
    DeleteAccountRequest,
    ForgotPasswordRequest,
    ForgotPasswordResponse,
    GoogleAuthCallbackRequest,
    GoogleAuthStartOut,
    LoginRequest,
    LogoutAllResponse,
    RegisterRequest,
    RegisterResponse,
    ResendVerificationRequest,
    ResendVerificationResponse,
    ResetPasswordRequest,
    ResetPasswordResponse,
    SessionInfoOut,
    SetPasswordRequest,
    VerifyEmailRequest,
)
from trip_api.security import (
    SsrfBlockedError,
    decrypt_secret,
    generate_opaque_token,
    generate_pkce_pair,
    hash_password,
    hash_session_token,
    validate_outbound_url,
    verify_password,
)
from trip_api.services import (
    stage_chart_cache_key,
    stage_events_cache_key,
    stage_lod_cache_key,
    stage_timeline_cache_key,
    user_to_out,
)
from trip_api.turnstile import verify_turnstile_token

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])
logger = logging.getLogger(__name__)

_DUMMY_ARGON2_HASH = hash_password("dummy-timing-guard-password-never-used")
_REGISTER_GENERIC_MESSAGE = (
    "If this email address is eligible, a verification link has been sent. "
    "Please check your inbox before signing in."
)


def _send_email_safe(
    kind: str,
    sender: Callable[[Settings, str, str, str], None],
    settings: Settings,
    recipient_email: str,
    display_name: str,
    action_url: str,
) -> None:
    try:
        sender(settings, recipient_email, display_name, action_url)
    except Exception as exc:
        logger.error("Failed to send %s email: %s", kind, type(exc).__name__)


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
    if user is None or user.password_hash is None:
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


@router.post("/register", response_model=RegisterResponse, status_code=202)
async def register(
    body: RegisterRequest,
    request: Request,
    background_tasks: BackgroundTasks,
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

    await verify_turnstile_token(
        settings=settings,
        request=request,
        token=body.turnstile_token,
        remote_ip=client_ip,
    )

    if "@" not in email or email.startswith("@") or email.endswith("@"):
        raise HTTPException(status_code=422, detail="Invalid email address")

    existing = db.scalar(select(User).where(User.email == email))
    if existing is not None:
        await asyncio.to_thread(verify_password, _DUMMY_ARGON2_HASH, body.password)
        if existing.is_active and existing.email_verified_at is None:
            raw_token = _issue_verification_token(db, settings, existing)
            db.add(
                AuditEvent(
                    user_id=existing.id,
                    event_type="auth.register_unverified_resend",
                    metadata_json={},
                )
            )
            db.commit()
            verify_url = build_verification_url(settings.public_base_url, raw_token)
            background_tasks.add_task(
                _send_email_safe,
                "verification",
                send_verification_email,
                settings,
                existing.email,
                existing.display_name,
                verify_url,
            )
        else:
            db.add(
                AuditEvent(
                    user_id=existing.id,
                    event_type="auth.register_existing_ignored",
                    metadata_json={},
                )
            )
            db.commit()

        return RegisterResponse(
            status="verification_required",
            email=email,
            message=_REGISTER_GENERIC_MESSAGE,
        )

    pw_hash = await asyncio.to_thread(hash_password, body.password)
    user = User(
        email=email,
        display_name=body.display_name.strip()[:120],
        password_hash=pw_hash,
        is_active=True,
        email_verified_at=None,
    )
    db.add(user)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        return RegisterResponse(
            status="verification_required",
            email=email,
            message=_REGISTER_GENERIC_MESSAGE,
        )

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
    background_tasks.add_task(
        _send_email_safe,
        "verification",
        send_verification_email,
        settings,
        user.email,
        user.display_name,
        verify_url,
    )

    return RegisterResponse(
        status="verification_required",
        email=user.email,
        message=_REGISTER_GENERIC_MESSAGE,
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
    background_tasks: BackgroundTasks,
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
        background_tasks.add_task(
            _send_email_safe,
            "verification",
            send_verification_email,
            settings,
            user.email,
            user.display_name,
            verify_url,
        )

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
async def forgot_password(
    body: ForgotPasswordRequest,
    request: Request,
    background_tasks: BackgroundTasks,
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

    await verify_turnstile_token(
        settings=settings,
        request=request,
        token=body.turnstile_token,
        remote_ip=client_ip,
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
        background_tasks.add_task(
            _send_email_safe,
            "password reset",
            send_password_reset_email,
            settings,
            user.email,
            user.display_name,
            reset_url,
        )

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
    if auth.user.password_hash is None:
        raise HTTPException(
            status_code=400,
            detail="Account does not have a password set yet. Use set-password instead.",
        )

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


@router.post("/set-password", response_model=ChangePasswordResponse)
def set_password(
    body: SetPasswordRequest,
    auth: CsrfAuth,
    db: DbSession,
) -> ChangePasswordResponse:
    if auth.user.password_hash is not None:
        raise HTTPException(status_code=400, detail="A password is already set on this account")

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
            event_type="auth.password_set",
            metadata_json={"revoked_sessions_count": revoked_count},
        )
    )
    db.commit()
    return ChangePasswordResponse(ok=True, revoked_sessions_count=revoked_count)


def _clear_google_oidc_cookie_headers(settings: Settings) -> dict[str, str]:
    secure_part = "; Secure" if settings.app_env == "production" else ""
    return {
        "Set-Cookie": (
            f"{GOOGLE_OIDC_COOKIE_NAME}=; Max-Age=0; Path={GOOGLE_OIDC_COOKIE_PATH}; "
            f"HttpOnly; SameSite=lax{secure_part}"
        )
    }


@router.get("/google/start", response_model=GoogleAuthStartOut)
def google_auth_start(
    request: Request,
    response: Response,
    db: DbSession,
    settings: AppSettings,
) -> GoogleAuthStartOut:
    client_ip = get_client_ip(request)
    check_rate_limit(
        db,
        bucket_key=f"google_auth:ip:{client_ip}",
        limit=settings.rate_limit_google_auth_max,
        window_seconds=settings.rate_limit_google_auth_window_s,
    )

    if not settings.google_auth_enabled:
        raise HTTPException(status_code=503, detail="Google sign-in is not configured")

    state = generate_opaque_token(32)
    nonce = generate_opaque_token(32)
    code_verifier, code_challenge = generate_pkce_pair()
    expires_at = int(datetime.now(UTC).timestamp()) + GOOGLE_OIDC_STATE_TTL_SECONDS

    cookie_value = encode_oidc_state_cookie(
        GoogleOidcState(
            state=state,
            nonce=nonce,
            code_verifier=code_verifier,
            expires_at=expires_at,
        ),
        settings,
    )
    response.set_cookie(
        key=GOOGLE_OIDC_COOKIE_NAME,
        value=cookie_value,
        httponly=True,
        samesite="lax",
        secure=settings.app_env == "production",
        path=GOOGLE_OIDC_COOKIE_PATH,
        max_age=GOOGLE_OIDC_STATE_TTL_SECONDS,
    )
    return GoogleAuthStartOut(
        authorize_url=build_google_authorize_url(
            settings=settings,
            state=state,
            nonce=nonce,
            code_challenge=code_challenge,
        )
    )


@router.post("/google/callback", response_model=SessionInfoOut)
async def google_auth_callback(
    body: GoogleAuthCallbackRequest,
    request: Request,
    response: Response,
    db: DbSession,
    settings: AppSettings,
) -> SessionInfoOut:
    client_ip = get_client_ip(request)
    check_rate_limit(
        db,
        bucket_key=f"google_auth:ip:{client_ip}",
        limit=settings.rate_limit_google_auth_max,
        window_seconds=settings.rate_limit_google_auth_window_s,
    )

    clear_headers = _clear_google_oidc_cookie_headers(settings)
    if not settings.google_auth_enabled:
        raise HTTPException(
            status_code=503,
            detail="Google sign-in is not configured",
            headers=clear_headers,
        )

    oidc_state = decode_oidc_state_cookie(
        request.cookies.get(GOOGLE_OIDC_COOKIE_NAME),
        settings,
    )
    if oidc_state is None or not hmac.compare_digest(
        oidc_state.state.encode("utf-8"), body.state.encode("utf-8")
    ):
        raise HTTPException(
            status_code=400,
            detail="Invalid or expired Google sign-in state",
            headers=clear_headers,
        )

    try:
        claims = await exchange_google_code(
            request=request,
            settings=settings,
            code=body.code,
            code_verifier=oidc_state.code_verifier,
            expected_nonce=oidc_state.nonce,
        )
    except HTTPException as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail=exc.detail,
            headers=clear_headers,
        ) from exc

    identity = db.scalar(
        select(UserIdentity).where(
            UserIdentity.provider == "google",
            UserIdentity.provider_subject == claims.subject,
        )
    )
    if identity is not None:
        user = db.get(User, identity.user_id)
        if user is None or not user.is_active:
            raise HTTPException(status_code=401, detail="Account inactive", headers=clear_headers)
        if identity.email != claims.email:
            identity.email = claims.email
        db.add(AuditEvent(user_id=user.id, event_type="auth.google_login", metadata_json={}))
        response.delete_cookie(
            key=GOOGLE_OIDC_COOKIE_NAME,
            path=GOOGLE_OIDC_COOKIE_PATH,
            httponly=True,
            samesite="lax",
            secure=settings.app_env == "production",
        )
        return _issue_session(db=db, response=response, settings=settings, user=user)

    existing_user = db.scalar(select(User).where(User.email == claims.email))
    if existing_user is None:
        if not settings.registration_enabled:
            raise HTTPException(
                status_code=403,
                detail="Account registration is disabled",
                headers=clear_headers,
            )
        now = datetime.now(UTC)
        new_user = User(
            email=claims.email,
            display_name=claims.display_name,
            password_hash=None,
            is_active=True,
            email_verified_at=now,
        )
        db.add(new_user)
        try:
            db.flush()
            db.add(
                UserIdentity(
                    user_id=new_user.id,
                    provider="google",
                    provider_subject=claims.subject,
                    email=claims.email,
                )
            )
            db.add(
                AuditEvent(user_id=new_user.id, event_type="auth.google_register", metadata_json={})
            )
            response.delete_cookie(
                key=GOOGLE_OIDC_COOKIE_NAME,
                path=GOOGLE_OIDC_COOKIE_PATH,
                httponly=True,
                samesite="lax",
                secure=settings.app_env == "production",
            )
            return _issue_session(db=db, response=response, settings=settings, user=new_user)
        except IntegrityError as exc:
            db.rollback()
            raise HTTPException(
                status_code=409,
                detail="Account creation conflicted with an existing sign-in. Please try again.",
                headers=clear_headers,
            ) from exc

    if not existing_user.is_active:
        raise HTTPException(status_code=401, detail="Account inactive", headers=clear_headers)

    if existing_user.email_verified_at is None:
        db.add(
            AuditEvent(
                user_id=existing_user.id,
                event_type="auth.google_link_blocked_unverified",
                metadata_json={},
            )
        )
        db.commit()
        raise HTTPException(
            status_code=409,
            detail=(
                "An unverified account with this email already exists. "
                "Please verify your email or reset your password before signing in with Google."
            ),
            headers=clear_headers,
        )

    db.add(
        UserIdentity(
            user_id=existing_user.id,
            provider="google",
            provider_subject=claims.subject,
            email=claims.email,
        )
    )
    db.add(AuditEvent(user_id=existing_user.id, event_type="auth.google_linked", metadata_json={}))
    try:
        response.delete_cookie(
            key=GOOGLE_OIDC_COOKIE_NAME,
            path=GOOGLE_OIDC_COOKIE_PATH,
            httponly=True,
            samesite="lax",
            secure=settings.app_env == "production",
        )
        return _issue_session(db=db, response=response, settings=settings, user=existing_user)
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="Google account linking conflicted with another request. Please try again.",
            headers=clear_headers,
        ) from exc


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


async def _deauthorize_strava_best_effort(
    request: Request,
    settings: Settings,
    access_token: str,
) -> None:
    custom_factory = getattr(request.app.state, "http_client_factory", None)
    url = f"{settings.strava_api_base_url.rstrip('/')}/oauth/deauthorize"
    try:
        validate_outbound_url(
            url,
            allowed_hosts=settings.strava_allowed_hosts,
            resolve_dns=(custom_factory is None),
        )
    except SsrfBlockedError:
        logger.warning("Skipped Strava deauthorization for disallowed outbound URL")
        return

    client_factory: Callable[[], httpx.AsyncClient] = (
        cast(Callable[[], httpx.AsyncClient], custom_factory)
        if custom_factory is not None
        else (lambda: httpx.AsyncClient(timeout=10.0, follow_redirects=False))
    )
    try:
        async with client_factory() as client:
            await client.post(
                url,
                data={"access_token": access_token},
                follow_redirects=False,
            )
    except Exception:
        logger.warning("Best-effort Strava deauthorization failed during account deletion")


@router.delete("/me", status_code=204)
async def delete_account(
    request: Request,
    auth: CsrfAuth,
    db: DbSession,
    settings: AppSettings,
    store: StoreDep,
    body: Annotated[DeleteAccountRequest | None, Body()] = None,
) -> Response:
    user = auth.user
    if user.password_hash is not None:
        provided_password = body.current_password if body is not None else None
        if not provided_password or not await asyncio.to_thread(
            verify_password, user.password_hash, provided_password
        ):
            raise HTTPException(status_code=400, detail="Current password is incorrect")
    else:
        confirmation = (body.confirmation or "").strip() if body is not None else ""
        if confirmation != "DELETE":
            raise HTTPException(
                status_code=400,
                detail='Type "DELETE" to confirm permanent account deletion',
            )

    user_id = user.id
    strava_access_token: str | None = None
    strava_conn = db.scalar(
        select(OAuthConnection).where(
            OAuthConnection.user_id == user_id,
            OAuthConnection.provider == "strava",
        )
    )
    if strava_conn is not None:
        try:
            strava_access_token = decrypt_secret(
                strava_conn.access_token_enc,
                settings.decoded_encryption_key,
                aad=f"oauth:{user_id}:strava".encode(),
            )
        except Exception:
            logger.warning("Could not decrypt Strava access token during account deletion")

    project_ids = list(db.scalars(select(Project.id).where(Project.owner_user_id == user_id)).all())
    stages = (
        list(db.scalars(select(Stage).where(Stage.project_id.in_(project_ids))).all())
        if project_ids
        else []
    )
    photos = (
        list(db.scalars(select(ProjectPhoto).where(ProjectPhoto.project_id.in_(project_ids))).all())
        if project_ids
        else []
    )
    render_jobs = list(
        db.scalars(
            select(RenderJob).where(
                (RenderJob.owner_user_id == user_id)
                | (RenderJob.project_id.in_(project_ids) if project_ids else False)
            )
        ).all()
    )
    render_job_ids = [job.id for job in render_jobs]
    render_chunks = (
        list(
            db.scalars(
                select(RenderJobChunk).where(RenderJobChunk.render_job_id.in_(render_job_ids))
            ).all()
        )
        if render_job_ids
        else []
    )
    vault_activities = list(
        db.scalars(select(VaultActivity).where(VaultActivity.user_id == user_id)).all()
    )

    keys_to_delete: list[str] = []
    for stage in stages:
        keys_to_delete.extend(
            [
                stage.bronze_object_key,
                stage.silver_object_key,
                stage_lod_cache_key(stage.project_id, stage.id),
                stage_chart_cache_key(stage.project_id, stage.id),
                stage_timeline_cache_key(stage.project_id, stage.id),
                stage_events_cache_key(stage.project_id, stage.id),
            ]
        )
    for photo in photos:
        keys_to_delete.extend([photo.object_key, photo.thumb_object_key])
    remote_executions_to_cancel: list[str] = []
    remote_jobs_to_cleanup: list[uuid.UUID] = []
    for job in render_jobs:
        if job.output_object_key:
            keys_to_delete.append(job.output_object_key)
        if job.thumbnail_key:
            keys_to_delete.append(job.thumbnail_key)
        if job.is_chunked:
            keys_to_delete.append(f"outputs/manifests/{job.id}.json")
        if job.render_backend == "aws_sfn_lambda":
            remote_jobs_to_cleanup.append(job.id)
            if job.external_execution_id and job.status in ("queued", "running"):
                remote_executions_to_cancel.append(job.external_execution_id)
    for chunk in render_chunks:
        if chunk.chunk_object_key:
            keys_to_delete.append(chunk.chunk_object_key)
    for activity in vault_activities:
        if activity.bronze_object_key:
            keys_to_delete.append(activity.bronze_object_key)
        if activity.silver_object_key:
            keys_to_delete.append(activity.silver_object_key)

    db.execute(delete(LibraryView).where(LibraryView.user_id == user_id))
    db.execute(delete(VaultActivityTile).where(VaultActivityTile.user_id == user_id))
    db.execute(delete(VaultActivity).where(VaultActivity.user_id == user_id))
    db.execute(delete(VaultSyncState).where(VaultSyncState.user_id == user_id))
    db.execute(delete(VaultGear).where(VaultGear.user_id == user_id))
    if render_job_ids:
        db.execute(delete(RenderJobChunk).where(RenderJobChunk.render_job_id.in_(render_job_ids)))
        db.execute(delete(RenderJob).where(RenderJob.id.in_(render_job_ids)))
    if project_ids:
        db.execute(delete(ProjectPhoto).where(ProjectPhoto.project_id.in_(project_ids)))
        db.execute(delete(Stage).where(Stage.project_id.in_(project_ids)))
        db.execute(delete(Project).where(Project.id.in_(project_ids)))
    db.execute(delete(OAuthState).where(OAuthState.user_id == user_id))
    db.execute(delete(OAuthConnection).where(OAuthConnection.user_id == user_id))
    db.execute(delete(UserIdentity).where(UserIdentity.user_id == user_id))
    db.execute(delete(EmailVerificationToken).where(EmailVerificationToken.user_id == user_id))
    db.execute(delete(PasswordResetToken).where(PasswordResetToken.user_id == user_id))
    db.execute(delete(SessionRecord).where(SessionRecord.user_id == user_id))
    db.execute(
        update(AuditEvent)
        .where(AuditEvent.user_id == user_id)
        .values(user_id=None, project_id=None)
    )
    db.add(AuditEvent(user_id=None, event_type="auth.account_deleted", metadata_json={}))
    db.delete(user)
    db.commit()

    if strava_access_token is not None:
        await _deauthorize_strava_best_effort(request, settings, strava_access_token)

    render_backend = getattr(request.app.state, "render_backend", None)
    if render_backend is not None:
        for exec_id in remote_executions_to_cancel:
            try:
                await asyncio.to_thread(render_backend.cancel, exec_id)
            except Exception:
                logger.warning("Failed to cancel remote render execution during account deletion")
        for job_id in remote_jobs_to_cleanup:
            try:
                await asyncio.to_thread(render_backend.cleanup_staging, job_id)
            except Exception:
                logger.warning("Failed to clean up remote render staging during account deletion")

    for obj_key in dict.fromkeys(keys_to_delete):
        try:
            await asyncio.to_thread(store.delete, obj_key)
        except Exception:
            logger.warning("Failed to delete object key %s during account cleanup", obj_key)

    resp = Response(status_code=204)
    resp.delete_cookie(
        key=settings.session_cookie_name,
        path="/",
        httponly=True,
        samesite="lax",
        secure=settings.app_env == "production",
    )
    return resp
