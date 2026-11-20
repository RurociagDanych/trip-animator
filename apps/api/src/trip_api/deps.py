"""FastAPI dependency providers for settings, DB sessions, auth, CSRF, and ObjectStore."""

from __future__ import annotations

from collections.abc import Generator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, cast

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from trip_api.config import Settings
from trip_api.db import SessionRecord, User
from trip_api.security import hash_session_token, verify_csrf_token
from trip_api.storage import ObjectStore


@dataclass(frozen=True, slots=True)
class AuthContext:
    """Authenticated session and user principal for the current request."""

    session: SessionRecord
    user: User


def ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def get_settings_dep(request: Request) -> Settings:
    return cast(Settings, request.app.state.settings)


def get_db(request: Request) -> Generator[Session, None, None]:
    session_factory = cast(sessionmaker[Session], request.app.state.session_factory)
    db = session_factory()
    try:
        yield db
    finally:
        db.close()


def get_object_store(request: Request) -> ObjectStore:
    return cast(ObjectStore, request.app.state.object_store)


def get_client_ip(request: Request) -> str:
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


AppSettings = Annotated[Settings, Depends(get_settings_dep)]
DbSession = Annotated[Session, Depends(get_db)]
StoreDep = Annotated[ObjectStore, Depends(get_object_store)]


def authenticate_request(
    request: Request,
    db: Session,
    settings: Settings,
) -> AuthContext:
    """Validate opaque session cookie against hashed DB session and active user."""
    raw_token = request.cookies.get(settings.session_cookie_name)
    if not raw_token:
        raise HTTPException(status_code=401, detail="Authentication required")

    token_hash = hash_session_token(raw_token, settings.session_secret)
    session_row = db.scalar(select(SessionRecord).where(SessionRecord.token_hash == token_hash))
    if session_row is None:
        raise HTTPException(status_code=401, detail="Invalid or expired session")

    if ensure_utc(session_row.expires_at) <= datetime.now(UTC):
        db.delete(session_row)
        db.commit()
        raise HTTPException(status_code=401, detail="Session expired")

    user = db.get(User, session_row.user_id)
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail="Account inactive or missing")

    return AuthContext(session=session_row, user=user)


def get_current_auth(
    request: Request,
    db: DbSession,
    settings: AppSettings,
) -> AuthContext:
    return authenticate_request(request, db, settings)


CurrentAuth = Annotated[AuthContext, Depends(get_current_auth)]


def require_csrf(
    request: Request,
    auth: CurrentAuth,
) -> AuthContext:
    """Enforce constant-time X-CSRF-Token verification on state-changing routes."""
    provided = request.headers.get("X-CSRF-Token")
    if not verify_csrf_token(auth.session.csrf_token, provided):
        raise HTTPException(status_code=403, detail="CSRF token missing or invalid")
    return auth


CsrfAuth = Annotated[AuthContext, Depends(require_csrf)]
