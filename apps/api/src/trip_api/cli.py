"""CLI utilities for Trip Animator API administration, Bronze->Silver replay, and ingest queue."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import hashlib
import json
import logging
import sys
import tempfile
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import func, select
from trip_telemetry import (
    ParsedTrack,
    build_lod_cache_payload,
    build_silver_dataframe,
    build_stage_events_parquet,
    infer_activity_type_from_strava,
    parse_strava_streams,
    parse_track_with_timeout,
    write_silver_parquet,
)

from trip_api.config import Settings, get_settings
from trip_api.db import (
    IngestJob,
    OAuthConnection,
    Project,
    Stage,
    User,
    create_db_engine,
    create_session_factory,
)
from trip_api.deps import ensure_utc
from trip_api.reaper import build_object_store, run_reap
from trip_api.security import decrypt_secret, encrypt_secret, hash_password
from trip_api.services import (
    build_stage_chart_cache_bytes,
    current_etl_version,
    ingest_parsed_track,
    stage_chart_cache_key,
    stage_events_cache_key,
    stage_lod_cache_key,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ReplayReport:
    """Summary counts produced by a Bronze -> Silver replay pass."""

    matched_stages: int
    replayed_stages: int
    failed_stages: int
    target_etl_version: int
    dry_run: bool = False


@dataclass(frozen=True, slots=True)
class IngestProcessReport:
    """Summary counts produced by processing queued Strava webhook ingest jobs."""

    matched_jobs: int
    succeeded_jobs: int
    ignored_jobs: int
    failed_jobs: int
    dry_run: bool = False


def create_user(
    *,
    email: str,
    password: str,
    display_name: str = "",
    database_url: str | None = None,
) -> User:
    """Provision an active user account with an Argon2id password hash."""
    normalized_email = email.strip().lower()
    if "@" not in normalized_email or len(normalized_email) > 320:
        raise ValueError("Invalid email address")

    pw_hash = hash_password(password)
    resolved_url = database_url or get_settings().database_url
    engine = create_db_engine(resolved_url)
    session_factory = create_session_factory(engine)
    try:
        with session_factory() as db:
            existing = db.scalar(select(User).where(User.email == normalized_email))
            if existing is not None:
                raise ValueError(f"User with email {normalized_email!r} already exists")
            user = User(
                email=normalized_email,
                display_name=(display_name.strip() or normalized_email.split("@", 1)[0])[:120],
                password_hash=pw_hash,
                is_active=True,
            )
            db.add(user)
            db.commit()
            db.refresh(user)
            return user
    finally:
        engine.dispose()


def _parse_bronze_for_replay(stage: Stage, bronze_bytes: bytes) -> ParsedTrack:
    fmt = getattr(stage, "source_format", None) or (
        "strava" if stage.source_type == "strava" else "gpx"
    )
    if fmt == "strava" or stage.source_type == "strava":
        payload = json.loads(bronze_bytes.decode("utf-8"))
        activity = payload.get("activity") or {}
        streams = payload.get("streams") or {}
        return parse_strava_streams(
            streams,
            start_date=activity.get("start_date"),
            name=str(activity.get("name") or stage.name),
        )
    return parse_track_with_timeout(
        bronze_bytes,
        format_hint=fmt,
        filename=stage.bronze_object_key,
        stitch_tracks=True,
    )


def run_replay_silver(
    settings: Settings | None = None,
    *,
    project_id: uuid.UUID | None = None,
    stage_id: uuid.UUID | None = None,
    target_etl_version: int | None = None,
    dry_run: bool = False,
    database_url: str | None = None,
) -> ReplayReport:
    """Deterministically replay Bronze to Silver Parquet + caches when etl_version advances."""
    cfg = settings or Settings()
    resolved_url = database_url or cfg.database_url
    target_ver = target_etl_version if target_etl_version is not None else current_etl_version()

    engine = create_db_engine(resolved_url)
    sf = create_session_factory(engine)
    store = build_object_store(cfg)
    replayed = 0
    failed = 0
    try:
        with sf() as db:
            stmt = select(Stage).where(Stage.etl_version < target_ver)
            if project_id is not None:
                stmt = stmt.where(Stage.project_id == project_id)
            if stage_id is not None:
                stmt = stmt.where(Stage.id == stage_id)
            stages = list(db.scalars(stmt.order_by(Stage.created_at.asc())).all())
            if dry_run:
                return ReplayReport(
                    matched_stages=len(stages),
                    replayed_stages=0,
                    failed_stages=0,
                    target_etl_version=target_ver,
                    dry_run=True,
                )

            for stage in stages:
                try:
                    bronze_bytes = store.get_bytes(stage.bronze_object_key)
                    parsed = _parse_bronze_for_replay(stage, bronze_bytes)
                    df, summary = build_silver_dataframe(
                        parsed,
                        trip_id=str(stage.project_id),
                        stage_id=str(stage.id),
                        trim_start_s=stage.trim_start_s,
                        trim_end_s=stage.trim_end_s,
                    )
                    with tempfile.TemporaryDirectory(prefix="trip_replay_") as tmpdir:
                        p_path = write_silver_parquet(df, Path(tmpdir) / f"{stage.id}.parquet")
                        store.put_bytes(
                            stage.silver_object_key,
                            p_path.read_bytes(),
                            content_type="application/vnd.apache.parquet",
                        )
                        chart_gz = build_stage_chart_cache_bytes(
                            p_path,
                            trim_start_s=stage.trim_start_s,
                            trim_end_s=stage.trim_end_s,
                        )
                        store.put_bytes(
                            stage_chart_cache_key(stage.project_id, stage.id),
                            chart_gz,
                            content_type="application/gzip",
                        )
                        ev_path = Path(tmpdir) / f"{stage.id}_events.parquet"
                        build_stage_events_parquet(
                            p_path,
                            ev_path,
                            stage_id=str(stage.id),
                            trim_start_s=stage.trim_start_s,
                            trim_end_s=stage.trim_end_s,
                        )
                        if ev_path.exists():
                            store.put_bytes(
                                stage_events_cache_key(stage.project_id, stage.id),
                                ev_path.read_bytes(),
                                content_type="application/vnd.apache.parquet",
                            )
                    lod_gz = build_lod_cache_payload(
                        parsed.points,
                        trim_start_s=stage.trim_start_s,
                        trim_end_s=stage.trim_end_s,
                    )
                    store.put_bytes(
                        stage_lod_cache_key(stage.project_id, stage.id),
                        lod_gz,
                        content_type="application/gzip",
                    )

                    stage.etl_version = target_ver
                    if not stage.bronze_sha256:
                        stage.bronze_sha256 = hashlib.sha256(bronze_bytes).hexdigest()
                    stage.point_count = summary.point_count
                    stage.distance_m = summary.distance_m
                    stage.elevation_gain_m = summary.elevation_gain_m
                    stage.elevation_loss_m = summary.elevation_loss_m
                    stage.moving_seconds = summary.moving_seconds
                    stage.elapsed_seconds = summary.elapsed_seconds
                    stage.min_lat = summary.min_lat
                    stage.max_lat = summary.max_lat
                    stage.min_lon = summary.min_lon
                    stage.max_lon = summary.max_lon
                    db.commit()
                    replayed += 1
                except Exception as exc:
                    db.rollback()
                    logger.warning("Replay failed for stage %s: %s", stage.id, exc)
                    failed += 1

            return ReplayReport(
                matched_stages=len(stages),
                replayed_stages=replayed,
                failed_stages=failed,
                target_etl_version=target_ver,
                dry_run=False,
            )
    finally:
        engine.dispose()


def _respect_strava_rate_limit(resp: httpx.Response) -> bool:
    """Return True if Strava 15m/daily usage header indicates we should pause further calls."""
    usage = resp.headers.get("X-RateLimit-Usage", "")
    limit = resp.headers.get("X-RateLimit-Limit", "")
    try:
        u15, uday = (int(x.strip()) for x in usage.split(",", 1))
        l15, lday = (int(x.strip()) for x in limit.split(",", 1))
        return u15 >= max(l15 - 2, 1) or uday >= max(lday - 5, 1)
    except Exception:
        return resp.status_code == 429


async def _fetch_strava_activity_and_streams(
    cfg: Settings,
    db: Any,
    conn: OAuthConnection,
    activity_id: int,
) -> tuple[dict[str, Any], dict[str, Any], bool]:
    key = cfg.decoded_encryption_key
    aad = f"oauth:{conn.user_id}:strava".encode()
    base = cfg.strava_api_base_url.rstrip("/")

    async with httpx.AsyncClient(timeout=15.0, follow_redirects=False) as client:
        if ensure_utc(conn.expires_at) <= datetime.now(UTC) + timedelta(seconds=60):
            ref = decrypt_secret(conn.refresh_token_enc, key, aad=aad)
            tok_resp = await client.post(
                f"{base}/oauth/token",
                data={
                    "client_id": cfg.strava_client_id,
                    "client_secret": cfg.strava_client_secret,
                    "grant_type": "refresh_token",
                    "refresh_token": ref,
                },
            )
            tok_resp.raise_for_status()
            tp = tok_resp.json()
            conn.access_token_enc = encrypt_secret(str(tp["access_token"]), key, aad=aad)
            conn.refresh_token_enc = encrypt_secret(
                str(tp.get("refresh_token") or ref), key, aad=aad
            )
            conn.expires_at = datetime.fromtimestamp(int(tp["expires_at"]), tz=UTC)
            db.commit()
            access_token = str(tp["access_token"])
        else:
            access_token = decrypt_secret(conn.access_token_enc, key, aad=aad)

        headers = {"Authorization": f"Bearer {access_token}"}
        act_resp = await client.get(f"{base}/api/v3/activities/{activity_id}", headers=headers)
        act_resp.raise_for_status()
        str_resp = await client.get(
            f"{base}/api/v3/activities/{activity_id}/streams",
            params={
                "keys": (
                    "latlng,time,altitude,heartrate,cadence,watts,"
                    "velocity_smooth,grade_smooth,moving,temp,distance"
                ),
                "key_by_type": "true",
            },
            headers=headers,
        )
        str_resp.raise_for_status()
        rate_limited = _respect_strava_rate_limit(str_resp)
        return act_resp.json(), str_resp.json(), rate_limited


def run_process_ingest_jobs(
    settings: Settings | None = None,
    *,
    limit: int = 20,
    dry_run: bool = False,
    database_url: str | None = None,
) -> IngestProcessReport:
    """Process queued Strava webhook IngestJobs using lease locking."""
    cfg = settings or Settings()
    resolved_url = database_url or cfg.database_url
    engine = create_db_engine(resolved_url)
    sf = create_session_factory(engine)
    store = build_object_store(cfg)

    succeeded = 0
    ignored = 0
    failed = 0
    now = datetime.now(UTC)

    try:
        with sf() as db:
            stmt = (
                select(IngestJob)
                .where(
                    (IngestJob.status == "queued")
                    | (
                        (IngestJob.status == "running")
                        & (IngestJob.lease_expires_at.is_not(None))
                        & (IngestJob.lease_expires_at <= now)
                    )
                )
                .order_by(IngestJob.created_at.asc())
                .limit(limit)
            )
            if resolved_url.startswith("postgresql"):
                stmt = stmt.with_for_update(skip_locked=True)
            jobs = list(db.scalars(stmt).all())
            if dry_run:
                return IngestProcessReport(
                    matched_jobs=len(jobs),
                    succeeded_jobs=0,
                    ignored_jobs=0,
                    failed_jobs=0,
                    dry_run=True,
                )

            for job in jobs:
                job.status = "running"
                job.attempt_count += 1
                job.lease_expires_at = datetime.now(UTC) + timedelta(minutes=5)
            db.commit()

            for job in jobs:
                try:
                    conns = list(
                        db.scalars(
                            select(OAuthConnection).where(
                                OAuthConnection.provider == job.provider,
                                OAuthConnection.external_athlete_id == job.external_owner_id,
                            )
                        ).all()
                    )
                    if not conns:
                        job.status = "ignored"
                        job.lease_expires_at = None
                        db.commit()
                        ignored += 1
                        continue

                    if job.event_type == "delete":
                        deleted_any = False
                        for conn in conns:
                            user_proj_ids = list(
                                db.scalars(
                                    select(Project.id).where(Project.owner_user_id == conn.user_id)
                                ).all()
                            )
                            if not user_proj_ids:
                                continue
                            matching_stages = list(
                                db.scalars(
                                    select(Stage).where(
                                        Stage.project_id.in_(user_proj_ids),
                                        Stage.strava_activity_id == job.external_object_id,
                                    )
                                ).all()
                            )
                            for st in matching_stages:
                                for k in (
                                    st.bronze_object_key,
                                    st.silver_object_key,
                                    stage_lod_cache_key(st.project_id, st.id),
                                    stage_chart_cache_key(st.project_id, st.id),
                                    stage_events_cache_key(st.project_id, st.id),
                                ):
                                    try:
                                        store.delete(k)
                                    except Exception:
                                        logger.debug("Cleanup skipped for key %s", k)
                                db.delete(st)
                                deleted_any = True
                        job.status = "succeeded" if deleted_any else "ignored"
                        job.lease_expires_at = None
                        db.commit()
                        if deleted_any:
                            succeeded += 1
                        else:
                            ignored += 1
                        continue

                    pause_rate_limit = False
                    for conn in conns:
                        act_json, str_json, hit_limit = asyncio.run(
                            _fetch_strava_activity_and_streams(
                                cfg, db, conn, int(job.external_object_id)
                            )
                        )
                        if hit_limit:
                            pause_rate_limit = True
                        proj = db.scalar(
                            select(Project)
                            .where(Project.owner_user_id == conn.user_id)
                            .order_by(Project.updated_at.desc())
                            .limit(1)
                        )
                        if proj is None:
                            proj = Project(
                                owner_user_id=conn.user_id,
                                title="Strava Synced Trips",
                                revision=1,
                                settings_json={},
                            )
                            db.add(proj)
                            db.flush()

                        parsed = parse_strava_streams(
                            str_json,
                            start_date=act_json.get("start_date"),
                            name=str(act_json.get("name") or "Strava Activity"),
                        )
                        raw_bronze = json.dumps(
                            {"activity": act_json, "streams": str_json},
                            separators=(",", ":"),
                            sort_keys=True,
                        ).encode("utf-8")
                        raw_sport = act_json.get("sport_type") or act_json.get("type")
                        act_type = infer_activity_type_from_strava(
                            str(raw_sport) if raw_sport is not None else None,
                            parsed.name,
                        )
                        max_day = db.scalar(
                            select(func.coalesce(func.max(Stage.day_index), 1)).where(
                                Stage.project_id == proj.id
                            )
                        )
                        ingest_parsed_track(
                            db=db,
                            store=store,
                            project=proj,
                            parsed=parsed,
                            raw_bronze_bytes=raw_bronze,
                            bronze_ext="json",
                            bronze_content_type="application/json",
                            source_type="strava",
                            source_format="strava",
                            stage_name=parsed.name or f"Strava {job.external_object_id}",
                            day_index=int(max_day or 1),
                            activity_type=act_type,
                            external_source_id=str(job.external_object_id),
                            strava_activity_id=int(job.external_object_id),
                        )
                    job.status = "succeeded"
                    job.lease_expires_at = None
                    db.commit()
                    succeeded += 1
                    if pause_rate_limit:
                        break
                except Exception as exc:
                    db.rollback()
                    job.status = "failed"
                    job.lease_expires_at = None
                    job.error_message = str(exc)[:500]
                    db.commit()
                    failed += 1

            return IngestProcessReport(
                matched_jobs=len(jobs),
                succeeded_jobs=succeeded,
                ignored_jobs=ignored,
                failed_jobs=failed,
                dry_run=False,
            )
    finally:
        engine.dispose()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="trip-api", description="Trip Animator API CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    cu = sub.add_parser("create-user", help="Create an active user with Argon2id password hash")
    cu.add_argument("--email", required=True, help="User email address")
    cu.add_argument("--password", default=None, help="User password (>= 12 chars)")
    cu.add_argument(
        "--password-stdin",
        action="store_true",
        help="Read password from standard input",
    )
    cu.add_argument("--display-name", default="", help="Optional display name")
    cu.add_argument("--database-url", default=None, help="Optional database URL override")

    reap_p = sub.add_parser(
        "reap",
        help="Prune expired sessions, OAuth states, stale rate limits, and expired render blobs",
    )
    reap_p.add_argument(
        "--dry-run",
        action="store_true",
        help="Report matching counts without deleting database rows or storage objects",
    )
    reap_p.add_argument(
        "--render-retention-days",
        type=int,
        default=None,
        help="Override RENDER_RETENTION_DAYS",
    )
    reap_p.add_argument(
        "--rate-limit-retention-hours",
        type=int,
        default=None,
        help="Override RATE_LIMIT_RETENTION_HOURS",
    )
    reap_p.add_argument("--database-url", default=None, help="Optional database URL override")
    reap_p.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Emit summary counts as JSON",
    )

    rep_p = sub.add_parser(
        "replay-silver",
        help="Deterministically replay Bronze telemetry into updated Silver Parquet + caches",
    )
    rep_p.add_argument("--project-id", default=None, help="Optional project UUID filter")
    rep_p.add_argument("--stage-id", default=None, help="Optional stage UUID filter")
    rep_p.add_argument(
        "--target-etl-version",
        type=int,
        default=None,
        help="Override target ETL version",
    )
    rep_p.add_argument("--dry-run", action="store_true", help="Count matching stages only")
    rep_p.add_argument("--database-url", default=None, help="Optional database URL override")
    rep_p.add_argument("--json", action="store_true", dest="as_json", help="Emit JSON output")

    ing_p = sub.add_parser(
        "process-ingest-jobs",
        help="Drain queued Strava webhook ingest_jobs",
    )
    ing_p.add_argument("--limit", type=int, default=20, help="Maximum jobs per run")
    ing_p.add_argument("--dry-run", action="store_true", help="Count matching jobs only")
    ing_p.add_argument("--database-url", default=None, help="Optional database URL override")
    ing_p.add_argument("--json", action="store_true", dest="as_json", help="Emit JSON output")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "create-user":
        password: str | None = args.password
        if args.password_stdin:
            password = sys.stdin.readline().rstrip("\r\n")
        elif password is None:
            password = getpass.getpass("Password (>= 12 chars): ")
        try:
            user = create_user(
                email=args.email,
                password=password,
                display_name=args.display_name,
                database_url=args.database_url,
            )
        except ValueError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        print(f"Created user id={user.id} email={user.email}")
        return 0

    if args.command == "reap":
        try:
            report = run_reap(
                dry_run=bool(args.dry_run),
                render_retention_days=args.render_retention_days,
                rate_limit_retention_hours=args.rate_limit_retention_hours,
                database_url=args.database_url,
            )
        except ValueError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1

        if args.as_json:
            print(
                json.dumps(
                    {
                        "sessions_deleted": report.sessions_deleted,
                        "oauth_states_deleted": report.oauth_states_deleted,
                        "rate_limits_deleted": report.rate_limits_deleted,
                        "render_jobs_deleted": report.render_jobs_deleted,
                        "blobs_deleted": report.blobs_deleted,
                        "dry_run": report.dry_run,
                    }
                )
            )
        else:
            print(
                f"Reap complete (dry_run={report.dry_run}): "
                f"sessions={report.sessions_deleted} "
                f"oauth_states={report.oauth_states_deleted} "
                f"rate_limits={report.rate_limits_deleted} "
                f"render_jobs={report.render_jobs_deleted} "
                f"blobs={report.blobs_deleted}"
            )
        return 0

    if args.command == "replay-silver":
        try:
            proj_uuid = uuid.UUID(args.project_id) if args.project_id else None
            stage_uuid = uuid.UUID(args.stage_id) if args.stage_id else None
            rep = run_replay_silver(
                project_id=proj_uuid,
                stage_id=stage_uuid,
                target_etl_version=args.target_etl_version,
                dry_run=bool(args.dry_run),
                database_url=args.database_url,
            )
        except ValueError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1

        if args.as_json:
            print(
                json.dumps(
                    {
                        "matched_stages": rep.matched_stages,
                        "replayed_stages": rep.replayed_stages,
                        "failed_stages": rep.failed_stages,
                        "target_etl_version": rep.target_etl_version,
                        "dry_run": rep.dry_run,
                    }
                )
            )
        else:
            print(
                f"Replay complete (dry_run={rep.dry_run}): matched={rep.matched_stages} "
                f"replayed={rep.replayed_stages} failed={rep.failed_stages}"
            )
        return 0 if rep.failed_stages == 0 else 1

    if args.command == "process-ingest-jobs":
        rep_ing = run_process_ingest_jobs(
            limit=args.limit,
            dry_run=bool(args.dry_run),
            database_url=args.database_url,
        )
        if args.as_json:
            print(
                json.dumps(
                    {
                        "matched_jobs": rep_ing.matched_jobs,
                        "succeeded_jobs": rep_ing.succeeded_jobs,
                        "ignored_jobs": rep_ing.ignored_jobs,
                        "failed_jobs": rep_ing.failed_jobs,
                        "dry_run": rep_ing.dry_run,
                    }
                )
            )
        else:
            print(
                f"Ingest jobs complete (dry_run={rep_ing.dry_run}): "
                f"matched={rep_ing.matched_jobs} succeeded={rep_ing.succeeded_jobs} "
                f"ignored={rep_ing.ignored_jobs} failed={rep_ing.failed_jobs}"
            )
        return 0 if rep_ing.failed_jobs == 0 else 1

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
