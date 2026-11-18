"""FastAPI application entrypoint with hardened headers, CORS, and RFC 7807 errors."""

from __future__ import annotations

import asyncio
import hmac
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy import func, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.formparsers import MultiPartException

from trip_api.config import Settings, get_settings
from trip_api.db import (
    OAuthConnection,
    Project,
    RenderJob,
    SessionRecord,
    Stage,
    create_db_engine,
    create_session_factory,
)
from trip_api.gold_sync import ensure_local_gold_duckdb
from trip_api.reaper import build_object_store
from trip_api.routers import (
    analytics,
    auth,
    photos,
    projects,
    renders,
    share,
    stages,
    strava,
    telemetry,
)
from trip_api.storage import ObjectStore

logger = logging.getLogger(__name__)


def _build_object_store(cfg: Settings) -> ObjectStore:
    return build_object_store(cfg)


def _problem_response(
    *,
    status_code: int,
    title: str,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "type": "about:blank",
            "title": title,
            "detail": title,
            "status": status_code,
        },
        headers=headers,
    )


def create_app(
    settings: Settings | None = None,
    *,
    engine: Engine | None = None,
    session_factory: sessionmaker[Session] | None = None,
    object_store: ObjectStore | None = None,
) -> FastAPI:
    cfg = settings or get_settings()

    db_engine = engine or create_db_engine(cfg.database_url, settings=cfg)
    db_session_factory = session_factory or create_session_factory(db_engine)
    store = object_store or _build_object_store(cfg)

    @asynccontextmanager
    async def _lifespan(_: FastAPI) -> AsyncIterator[None]:
        if cfg.object_store_backend == "s3":
            try:
                await asyncio.to_thread(ensure_local_gold_duckdb, store, cfg)
            except Exception as exc:
                logger.warning("Startup Gold DuckDB sync skipped: %s", type(exc).__name__)
        yield

    application = FastAPI(
        title="Trip Animator API",
        version="0.1.0",
        docs_url="/api/docs" if cfg.app_env != "production" else None,
        redoc_url=None,
        openapi_url="/api/openapi.json" if cfg.app_env != "production" else None,
        lifespan=_lifespan,
    )
    application.state.settings = cfg
    application.state.engine = db_engine
    application.state.session_factory = db_session_factory
    application.state.object_store = store

    application.add_middleware(
        CORSMiddleware,
        allow_origins=cfg.allowed_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "X-CSRF-Token", "Idempotency-Key"],
    )

    @application.middleware("http")
    async def security_headers_middleware(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "geolocation=(), camera=(), microphone=()"
        return response

    @application.exception_handler(StarletteHTTPException)
    async def http_exception_handler(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        safe_title = "Internal server error" if exc.status_code >= 500 else str(exc.detail)
        return _problem_response(
            status_code=exc.status_code,
            title=safe_title,
            headers=dict(exc.headers) if exc.headers else None,
        )

    @application.exception_handler(MultiPartException)
    async def multipart_exception_handler(_: Request, __: MultiPartException) -> JSONResponse:
        return _problem_response(
            status_code=400,
            title="Malformed multipart upload payload",
        )

    @application.exception_handler(RequestValidationError)
    async def validation_exception_handler(_: Request, __: RequestValidationError) -> JSONResponse:
        return _problem_response(
            status_code=422,
            title="Request validation failed",
        )

    @application.exception_handler(Exception)
    async def unhandled_exception_handler(_: Request, exc: Exception) -> JSONResponse:
        logger.error("Unhandled API exception: %s", type(exc).__name__)
        return _problem_response(
            status_code=500,
            title="Internal server error",
        )

    @application.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "service": "trip-api", "env": cfg.app_env}

    @application.get("/ready")
    async def ready() -> JSONResponse:
        try:
            with db_engine.connect() as conn:
                conn.execute(text("SELECT 1"))
        except SQLAlchemyError as exc:
            logger.warning("Readiness check failed: %s", type(exc).__name__)
            return JSONResponse(
                status_code=503,
                content={
                    "status": "not_ready",
                    "service": "trip-api",
                    "database": "unavailable",
                },
            )
        return JSONResponse(
            status_code=200,
            content={"status": "ready", "service": "trip-api", "database": "ok"},
        )

    @application.get("/metrics")
    async def metrics(request: Request) -> PlainTextResponse:
        auth_header = request.headers.get("Authorization", "")
        provided_token: str | None = None
        if auth_header.lower().startswith("bearer "):
            provided_token = auth_header[7:].strip()

        if cfg.metrics_token:
            if not provided_token:
                raise HTTPException(
                    status_code=401,
                    detail="Metrics authentication required",
                    headers={"WWW-Authenticate": "Bearer"},
                )
            if not hmac.compare_digest(
                provided_token.encode("utf-8"), cfg.metrics_token.encode("utf-8")
            ):
                raise HTTPException(status_code=403, detail="Invalid metrics token")
        elif cfg.app_env == "production":
            raise HTTPException(status_code=403, detail="Metrics endpoint is disabled")

        now = datetime.now(UTC)
        db_up = 1
        render_counts = {
            status_name: 0
            for status_name in ("queued", "running", "succeeded", "failed", "cancelled")
        }
        projects_total = 0
        stages_total = 0
        sessions_active = 0
        oauth_connections_total = 0

        try:
            with db_session_factory() as db:
                rows = db.execute(
                    select(RenderJob.status, func.count()).group_by(RenderJob.status)
                ).all()
                for status_val, count_val in rows:
                    key = str(status_val)
                    if key in render_counts:
                        render_counts[key] = int(count_val)
                projects_total = int(db.scalar(select(func.count()).select_from(Project)) or 0)
                stages_total = int(db.scalar(select(func.count()).select_from(Stage)) or 0)
                sessions_active = int(
                    db.scalar(
                        select(func.count())
                        .select_from(SessionRecord)
                        .where(SessionRecord.expires_at > now)
                    )
                    or 0
                )
                oauth_connections_total = int(
                    db.scalar(select(func.count()).select_from(OAuthConnection)) or 0
                )
        except SQLAlchemyError as exc:
            logger.warning("Metrics DB query failed: %s", type(exc).__name__)
            db_up = 0

        lines = [
            "# HELP trip_api_up Whether the Trip Animator API process is serving requests.",
            "# TYPE trip_api_up gauge",
            "trip_api_up 1",
            "# HELP trip_db_up Whether PostgreSQL/SQLite connectivity is healthy (1=up, 0=down).",
            "# TYPE trip_db_up gauge",
            f"trip_db_up {db_up}",
            "# HELP trip_render_jobs_total Total render jobs grouped by queue status.",
            "# TYPE trip_render_jobs_total gauge",
            *[
                f'trip_render_jobs_total{{status="{status_name}"}} {count_val}'
                for status_name, count_val in render_counts.items()
            ],
            "# HELP trip_projects_total Total persisted trip projects.",
            "# TYPE trip_projects_total gauge",
            f"trip_projects_total {projects_total}",
            "# HELP trip_stages_total Total persisted trip stages.",
            "# TYPE trip_stages_total gauge",
            f"trip_stages_total {stages_total}",
            "# HELP trip_sessions_active Active non-expired authenticated sessions.",
            "# TYPE trip_sessions_active gauge",
            f"trip_sessions_active {sessions_active}",
            "# HELP trip_oauth_connections_total Active OAuth provider connections.",
            "# TYPE trip_oauth_connections_total gauge",
            f"trip_oauth_connections_total {oauth_connections_total}",
        ]
        return PlainTextResponse(
            content="\n".join(lines) + "\n",
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )

    @application.get("/api/v1/public-config")
    async def public_config() -> dict[str, object]:
        return {
            "dev_auth_enabled": cfg.dev_auth_enabled and cfg.app_env != "production",
            "registration_enabled": cfg.registration_enabled,
            "strava_configured": bool(cfg.strava_client_id),
            "google_photos_configured": bool(cfg.google_client_id and cfg.google_client_secret),
        }

    application.include_router(auth.router)
    application.include_router(projects.router)
    application.include_router(stages.router)
    application.include_router(photos.router)
    application.include_router(telemetry.router)
    application.include_router(analytics.router)
    application.include_router(strava.router)
    application.include_router(renders.router)
    application.include_router(share.router)

    return application


app = create_app()
