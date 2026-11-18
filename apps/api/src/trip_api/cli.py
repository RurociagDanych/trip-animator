"""CLI utilities for Trip Animator API administration, Bronze->Silver replay, and ingest queue."""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import logging
import signal
import sys
import tempfile
import threading
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

import httpx
from sqlalchemy import select
from trip_telemetry import (
    ParsedTrack,
    build_lod_cache_payload,
    build_silver_dataframe,
    build_stage_events_parquet,
    parse_strava_streams,
    parse_track_with_timeout,
    write_silver_parquet,
)

from trip_api.config import Settings, get_settings
from trip_api.db import (
    Stage,
    User,
    create_db_engine,
    create_session_factory,
)
from trip_api.ingest import count_pending_ingest_jobs, process_pending_ingest_jobs
from trip_api.reaper import build_object_store, run_reap
from trip_api.security import hash_password
from trip_api.services import (
    build_stage_chart_cache_bytes,
    current_etl_version,
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
    deferred_jobs: int = 0


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


def run_process_ingest_jobs(
    settings: Settings | None = None,
    *,
    limit: int = 20,
    dry_run: bool = False,
    database_url: str | None = None,
    max_attempts: int = 5,
    http_client_factory: Callable[[], httpx.Client] | None = None,
) -> IngestProcessReport:
    """Process queued Strava webhook IngestJobs via canonical `trip_api.ingest`."""
    if limit < 1:
        raise ValueError("limit must be >= 1")
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")

    cfg = settings or Settings()
    resolved_url = database_url or cfg.database_url
    engine = create_db_engine(resolved_url)
    sf = create_session_factory(engine)
    store = build_object_store(cfg)

    try:
        with sf() as db:
            if dry_run:
                matched = count_pending_ingest_jobs(
                    db,
                    batch_size=limit,
                    max_attempts=max_attempts,
                )
                return IngestProcessReport(
                    matched_jobs=matched,
                    succeeded_jobs=0,
                    ignored_jobs=0,
                    failed_jobs=0,
                    dry_run=True,
                    deferred_jobs=0,
                )

            res = process_pending_ingest_jobs(
                db,
                store,
                cfg,
                batch_size=limit,
                max_attempts=max_attempts,
                http_client_factory=http_client_factory,
            )
            return IngestProcessReport(
                matched_jobs=int(res["claimed"]),
                succeeded_jobs=int(res["succeeded"]),
                ignored_jobs=int(res["ignored"]),
                failed_jobs=int(res["failed"]),
                dry_run=False,
                deferred_jobs=int(res.get("deferred", 0)),
            )
    finally:
        engine.dispose()


def run_ingest_worker_loop(
    settings: Settings | None = None,
    *,
    limit: int = 20,
    dry_run: bool = False,
    database_url: str | None = None,
    poll_interval_s: float = 5.0,
    max_attempts: int = 5,
    stop_event: threading.Event | None = None,
    max_iterations: int | None = None,
    http_client_factory: Callable[[], httpx.Client] | None = None,
) -> IngestProcessReport:
    """Continuously drain IngestJobs with SIGTERM/SIGINT graceful shutdown."""
    if poll_interval_s <= 0.0:
        raise ValueError("poll_interval_s must be > 0")
    if limit < 1:
        raise ValueError("limit must be >= 1")
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")

    cfg = settings or Settings()
    resolved_url = database_url or cfg.database_url
    engine = create_db_engine(resolved_url)
    sf = create_session_factory(engine)
    store = build_object_store(cfg)

    shutdown_event = stop_event or threading.Event()
    prev_sigterm: object = None
    prev_sigint: object = None
    in_main_thread = threading.current_thread() is threading.main_thread()

    def _handle_signal(_signum: int, _frame: object) -> None:
        shutdown_event.set()

    if in_main_thread:
        prev_sigterm = signal.getsignal(signal.SIGTERM)
        prev_sigint = signal.getsignal(signal.SIGINT)
        signal.signal(signal.SIGTERM, _handle_signal)
        signal.signal(signal.SIGINT, _handle_signal)

    total_matched = 0
    total_succeeded = 0
    total_ignored = 0
    total_failed = 0
    total_deferred = 0
    iterations = 0

    try:
        while not shutdown_event.is_set():
            with sf() as db:
                if dry_run:
                    matched = count_pending_ingest_jobs(
                        db,
                        batch_size=limit,
                        max_attempts=max_attempts,
                    )
                    total_matched += matched
                    claimed = 0
                    deferred = 0
                else:
                    res = process_pending_ingest_jobs(
                        db,
                        store,
                        cfg,
                        batch_size=limit,
                        max_attempts=max_attempts,
                        http_client_factory=http_client_factory,
                    )
                    claimed = int(res["claimed"])
                    deferred = int(res.get("deferred", 0))
                    total_matched += claimed
                    total_succeeded += int(res["succeeded"])
                    total_ignored += int(res["ignored"])
                    total_failed += int(res["failed"])
                    total_deferred += deferred

            iterations += 1
            if max_iterations is not None and iterations >= max_iterations:
                break
            if dry_run or claimed < limit or deferred > 0:
                shutdown_event.wait(timeout=poll_interval_s)
    finally:
        if in_main_thread:
            if prev_sigterm is not None:
                signal.signal(signal.SIGTERM, prev_sigterm)  # type: ignore[arg-type]
            if prev_sigint is not None:
                signal.signal(signal.SIGINT, prev_sigint)  # type: ignore[arg-type]
        engine.dispose()

    return IngestProcessReport(
        matched_jobs=total_matched,
        succeeded_jobs=total_succeeded,
        ignored_jobs=total_ignored,
        failed_jobs=total_failed,
        dry_run=dry_run,
        deferred_jobs=total_deferred,
    )


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
    ing_p.add_argument(
        "--loop",
        action="store_true",
        help="Continuously poll and drain queued ingest jobs until SIGTERM/SIGINT",
    )
    ing_p.add_argument(
        "--poll-interval-s",
        type=float,
        default=5.0,
        help="Polling interval in seconds when --loop is enabled",
    )
    ing_p.add_argument(
        "--max-attempts",
        type=int,
        default=5,
        help="Maximum processing attempts before marking an ingest job failed",
    )
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
        try:
            if args.loop:
                rep_ing = run_ingest_worker_loop(
                    limit=args.limit,
                    dry_run=bool(args.dry_run),
                    database_url=args.database_url,
                    poll_interval_s=args.poll_interval_s,
                    max_attempts=args.max_attempts,
                )
            else:
                rep_ing = run_process_ingest_jobs(
                    limit=args.limit,
                    dry_run=bool(args.dry_run),
                    database_url=args.database_url,
                    max_attempts=args.max_attempts,
                )
        except ValueError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1

        if args.as_json:
            print(
                json.dumps(
                    {
                        "matched_jobs": rep_ing.matched_jobs,
                        "succeeded_jobs": rep_ing.succeeded_jobs,
                        "ignored_jobs": rep_ing.ignored_jobs,
                        "failed_jobs": rep_ing.failed_jobs,
                        "deferred_jobs": rep_ing.deferred_jobs,
                        "dry_run": rep_ing.dry_run,
                    }
                )
            )
        else:
            print(
                f"Ingest jobs complete (dry_run={rep_ing.dry_run}): "
                f"matched={rep_ing.matched_jobs} succeeded={rep_ing.succeeded_jobs} "
                f"ignored={rep_ing.ignored_jobs} failed={rep_ing.failed_jobs} "
                f"deferred={rep_ing.deferred_jobs}"
            )
        return 0 if getattr(args, "loop", False) else (0 if rep_ing.failed_jobs == 0 else 1)

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
