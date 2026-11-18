from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import boto3
import pytest
from alembic import command
from alembic.config import Config
from botocore.exceptions import ClientError
from sqlalchemy import select
from trip_api.cli import main as cli_main
from trip_api.config import Settings
from trip_api.db import (
    OAuthState,
    Project,
    RateLimitRecord,
    RenderJob,
    SessionRecord,
    User,
    create_db_engine,
    create_session_factory,
)
from trip_api.reaper import build_object_store, reap_expired
from trip_api.storage import LocalObjectStore, S3ObjectStore

if TYPE_CHECKING:
    from conftest import AppHarness


def _seed_project(harness: AppHarness, email: str) -> tuple[User, Project]:
    user = harness.create_user(email)
    with harness.session_factory() as db:
        proj = Project(
            owner_user_id=user.id,
            title="Reaper Test Project",
            revision=1,
            settings_json={},
        )
        db.add(proj)
        db.commit()
        db.refresh(proj)
        return user, proj


def test_reap_expired_prunes_only_expired_records_and_blobs(harness: AppHarness) -> None:
    user, proj = _seed_project(harness, "reaper@example.com")
    now = datetime.now(UTC)

    expired_sess_id = uuid.uuid4()
    active_sess_id = uuid.uuid4()

    harness.store.put_bytes("outputs/exp1.mp4", b"mp4-exp1")
    harness.store.put_bytes("outputs/exp1.webp", b"webp-exp1")
    harness.store.put_bytes("outputs/exp2.mp4", b"mp4-exp2")
    harness.store.put_bytes("outputs/fresh.mp4", b"mp4-fresh")
    harness.store.put_bytes("outputs/fresh.webp", b"webp-fresh")

    with harness.session_factory() as db:
        db.add_all(
            [
                SessionRecord(
                    id=expired_sess_id,
                    user_id=user.id,
                    token_hash="h" * 64,
                    csrf_token="c1",
                    expires_at=now - timedelta(minutes=5),
                ),
                SessionRecord(
                    id=active_sess_id,
                    user_id=user.id,
                    token_hash="a" * 64,
                    csrf_token="c2",
                    expires_at=now + timedelta(hours=12),
                ),
                OAuthState(
                    user_id=user.id,
                    session_id=active_sess_id,
                    provider="strava",
                    state_hash="s1" * 32,
                    code_verifier_enc="v1:enc",
                    expires_at=now - timedelta(minutes=1),
                ),
                OAuthState(
                    user_id=user.id,
                    session_id=expired_sess_id,
                    provider="strava",
                    state_hash="s2" * 32,
                    code_verifier_enc="v1:enc",
                    expires_at=now + timedelta(minutes=5),
                ),
                OAuthState(
                    user_id=user.id,
                    session_id=active_sess_id,
                    provider="strava",
                    state_hash="s3" * 32,
                    code_verifier_enc="v1:enc",
                    expires_at=now + timedelta(minutes=10),
                ),
                RateLimitRecord(
                    bucket_key="login:stale",
                    window_start=now - timedelta(hours=48),
                    request_count=3,
                    updated_at=now - timedelta(hours=48),
                ),
                RateLimitRecord(
                    bucket_key="login:fresh",
                    window_start=now - timedelta(minutes=5),
                    request_count=1,
                    updated_at=now - timedelta(minutes=5),
                ),
                RenderJob(
                    project_id=proj.id,
                    owner_user_id=user.id,
                    idempotency_key="exp-ttl-1",
                    idempotency_hash="x" * 64,
                    spec_json={},
                    status="succeeded",
                    progress_pct=100,
                    attempt_count=1,
                    output_object_key="outputs/exp1.mp4",
                    thumbnail_key="outputs/exp1.webp",
                    expires_at=now - timedelta(hours=1),
                    created_at=now - timedelta(hours=2),
                    updated_at=now - timedelta(hours=2),
                ),
                RenderJob(
                    project_id=proj.id,
                    owner_user_id=user.id,
                    idempotency_key="exp-ret-2",
                    idempotency_hash="y" * 64,
                    spec_json={},
                    status="failed",
                    progress_pct=40,
                    attempt_count=3,
                    output_object_key="outputs/exp2.mp4",
                    thumbnail_key=None,
                    expires_at=None,
                    created_at=now - timedelta(days=10),
                    updated_at=now - timedelta(days=10),
                ),
                RenderJob(
                    project_id=proj.id,
                    owner_user_id=user.id,
                    idempotency_key="active-queued-3",
                    idempotency_hash="z" * 64,
                    spec_json={},
                    status="queued",
                    progress_pct=0,
                    attempt_count=0,
                    expires_at=None,
                    created_at=now - timedelta(days=20),
                    updated_at=now - timedelta(days=20),
                ),
                RenderJob(
                    project_id=proj.id,
                    owner_user_id=user.id,
                    idempotency_key="fresh-ok-4",
                    idempotency_hash="w" * 64,
                    spec_json={},
                    status="succeeded",
                    progress_pct=100,
                    attempt_count=1,
                    output_object_key="outputs/fresh.mp4",
                    thumbnail_key="outputs/fresh.webp",
                    expires_at=now + timedelta(days=5),
                    created_at=now - timedelta(days=1),
                    updated_at=now - timedelta(days=1),
                ),
            ]
        )
        db.commit()

        dry = reap_expired(
            db,
            harness.store,
            now=now,
            render_retention_days=7,
            rate_limit_retention_hours=24,
            dry_run=True,
        )
        assert dry.dry_run is True
        assert dry.sessions_deleted == 1
        assert dry.oauth_states_deleted == 2
        assert dry.rate_limits_deleted == 1
        assert dry.render_jobs_deleted == 2
        assert dry.blobs_deleted == 3
        assert harness.store.get_bytes("outputs/exp1.mp4") == b"mp4-exp1"

        res = reap_expired(
            db,
            harness.store,
            now=now,
            render_retention_days=7,
            rate_limit_retention_hours=24,
            dry_run=False,
        )
        assert res.dry_run is False
        assert res.sessions_deleted == 1
        assert res.oauth_states_deleted == 2
        assert res.rate_limits_deleted == 1
        assert res.render_jobs_deleted == 2
        assert res.blobs_deleted == 3

        remaining_sessions = db.scalars(select(SessionRecord)).all()
        assert [s.id for s in remaining_sessions] == [active_sess_id]

        remaining_states = db.scalars(select(OAuthState)).all()
        assert len(remaining_states) == 1
        assert remaining_states[0].state_hash == "s3" * 32

        remaining_limits = db.scalars(select(RateLimitRecord)).all()
        assert [r.bucket_key for r in remaining_limits] == ["login:fresh"]

        remaining_jobs = db.scalars(select(RenderJob)).all()
        assert {j.idempotency_key for j in remaining_jobs} == {
            "active-queued-3",
            "fresh-ok-4",
        }

    with pytest.raises(FileNotFoundError):
        harness.store.resolve_existing_path("outputs/exp1.mp4")
    with pytest.raises(FileNotFoundError):
        harness.store.resolve_existing_path("outputs/exp1.webp")
    with pytest.raises(FileNotFoundError):
        harness.store.resolve_existing_path("outputs/exp2.mp4")
    assert harness.store.get_bytes("outputs/fresh.mp4") == b"mp4-fresh"
    assert harness.store.get_bytes("outputs/fresh.webp") == b"webp-fresh"


def test_reap_keeps_render_job_if_blob_delete_fails(harness: AppHarness) -> None:
    class FailingStore(LocalObjectStore):
        def delete(self, key: str) -> None:
            raise RuntimeError("S3 outage")

    user, proj = _seed_project(harness, "failstore@example.com")
    now = datetime.now(UTC)
    harness.store.put_bytes("outputs/keepme.mp4", b"video-data")
    harness.store.put_bytes("outputs/keepme.webp", b"thumb-data")

    with harness.session_factory() as db:
        db.add(
            RenderJob(
                project_id=proj.id,
                owner_user_id=user.id,
                idempotency_key="fail-delete-1",
                idempotency_hash="f" * 64,
                spec_json={},
                status="succeeded",
                progress_pct=100,
                attempt_count=1,
                output_object_key="outputs/keepme.mp4",
                thumbnail_key="outputs/keepme.webp",
                expires_at=now - timedelta(days=1),
                created_at=now - timedelta(days=2),
                updated_at=now - timedelta(days=2),
            )
        )
        db.commit()

        failing_store = FailingStore(harness.store.root_dir)
        report = reap_expired(db, failing_store, now=now)
        assert report.render_jobs_deleted == 0
        assert report.blobs_deleted == 0
        assert len(db.scalars(select(RenderJob)).all()) == 1


def test_reap_orphan_jobs_missing_blobs_s3_and_validation(
    harness: AppHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user, proj = _seed_project(harness, "orphan@example.com")
    now = datetime.now(UTC)

    # Validation errors on non-positive retention arguments
    with harness.session_factory() as db:
        with pytest.raises(ValueError, match="render_retention_days"):
            reap_expired(db, harness.store, render_retention_days=0)
        with pytest.raises(ValueError, match="rate_limit_retention_hours"):
            reap_expired(db, harness.store, rate_limit_retention_hours=0)

    # Orphaned/expired terminal jobs whose blobs are already missing, plus an active running job
    with harness.session_factory() as db:
        db.add_all(
            [
                RenderJob(
                    project_id=proj.id,
                    owner_user_id=user.id,
                    idempotency_key="orphan-missing-local",
                    idempotency_hash="a" * 64,
                    spec_json={},
                    status="succeeded",
                    progress_pct=100,
                    attempt_count=1,
                    output_object_key="outputs/already-gone.mp4",
                    thumbnail_key="outputs/already-gone.webp",
                    expires_at=now - timedelta(minutes=1),
                    created_at=now - timedelta(hours=1),
                    updated_at=now - timedelta(hours=1),
                ),
                RenderJob(
                    project_id=proj.id,
                    owner_user_id=user.id,
                    idempotency_key="cancelled-old",
                    idempotency_hash="b" * 64,
                    spec_json={},
                    status="cancelled",
                    progress_pct=10,
                    attempt_count=1,
                    output_object_key=None,
                    thumbnail_key=None,
                    expires_at=None,
                    created_at=now - timedelta(days=9),
                    updated_at=now - timedelta(days=9),
                ),
                RenderJob(
                    project_id=proj.id,
                    owner_user_id=user.id,
                    idempotency_key="running-preserved",
                    idempotency_hash="c" * 64,
                    spec_json={},
                    status="running",
                    progress_pct=50,
                    attempt_count=1,
                    output_object_key=None,
                    thumbnail_key=None,
                    expires_at=now - timedelta(days=1),
                    created_at=now - timedelta(days=20),
                    updated_at=now - timedelta(days=20),
                ),
            ]
        )
        db.commit()

        # Pass naive datetime to exercise UTC normalization
        naive_now = now.replace(tzinfo=None)
        res = reap_expired(db, harness.store, now=naive_now, render_retention_days=7)
        assert res.render_jobs_deleted == 2
        assert res.blobs_deleted == 0
        remaining = db.scalars(select(RenderJob)).all()
        assert [r.idempotency_key for r in remaining] == ["running-preserved"]

    # Non-local store raising FileNotFoundError on delete is treated as safely absent
    class MissingOnDeleteStore(LocalObjectStore):
        def delete(self, key: str) -> None:
            raise FileNotFoundError(key)

    from trip_api.reaper import _delete_blob_safe

    ok, deleted = _delete_blob_safe(MissingOnDeleteStore(harness.store.root_dir), "outputs/x.mp4")
    assert ok is True
    assert deleted is False

    # S3ObjectStore factory and missing-key S3 cleanup
    deleted_s3_keys: list[str] = []

    class FakeS3Client:
        def delete_object(self, **kwargs: Any) -> None:
            key = str(kwargs["Key"])
            deleted_s3_keys.append(key)
            if key == "outputs/missing-s3.mp4":
                raise ClientError({"Error": {"Code": "NoSuchKey"}}, "DeleteObject")

    monkeypatch.setattr(boto3, "client", lambda *_args, **_kwargs: FakeS3Client())
    s3_cfg = Settings(
        app_env="test",
        object_store_backend="s3",
        s3_endpoint_url="http://localhost:9000",
        s3_access_key_id="k",
        s3_secret_access_key="s",
        s3_bucket="trip-test",
        s3_region="us-east-1",
    )
    built_store = build_object_store(s3_cfg)
    assert isinstance(built_store, S3ObjectStore)

    with harness.session_factory() as db:
        db.add(
            RenderJob(
                project_id=proj.id,
                owner_user_id=user.id,
                idempotency_key="s3-cleanup-job",
                idempotency_hash="d" * 64,
                spec_json={},
                status="failed",
                progress_pct=0,
                attempt_count=2,
                output_object_key="outputs/missing-s3.mp4",
                thumbnail_key="outputs/present-s3.webp",
                expires_at=now - timedelta(minutes=1),
                created_at=now - timedelta(days=1),
                updated_at=now - timedelta(days=1),
            )
        )
        db.commit()
        s3_res = reap_expired(db, built_store, now=now)
        assert s3_res.render_jobs_deleted == 1
        assert s3_res.blobs_deleted == 2
        assert deleted_s3_keys == ["outputs/missing-s3.mp4", "outputs/present-s3.webp"]


def test_cli_reap_dry_run_and_execute(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_file = tmp_path / "reap_cli.db"
    db_url = f"sqlite:///{db_file}"
    store_dir = tmp_path / "objects"

    api_dir = Path(__file__).resolve().parents[1]
    cfg = Config(str(api_dir / "alembic.ini"))
    cfg.set_main_option("script_location", str(api_dir / "alembic"))
    cfg.set_main_option("sqlalchemy.url", db_url)
    command.upgrade(cfg, "head")

    monkeypatch.setenv("OBJECT_STORE_BACKEND", "local")
    monkeypatch.setenv("LOCAL_OBJECT_STORE_DIR", str(store_dir))
    monkeypatch.setenv("DATABASE_URL", db_url)

    store = LocalObjectStore(store_dir)
    store.put_bytes("outputs/cli-exp.mp4", b"video-bytes")

    engine = create_db_engine(db_url)
    sf = create_session_factory(engine)
    now = datetime.now(UTC)
    try:
        with sf() as db:
            user = User(
                email="cli-reap@example.com",
                display_name="CLI Reap",
                password_hash="argon2-placeholder",
                is_active=True,
            )
            db.add(user)
            db.flush()
            proj = Project(
                owner_user_id=user.id,
                title="CLI Project",
                revision=1,
                settings_json={},
            )
            db.add(proj)
            db.flush()
            db.add(
                SessionRecord(
                    user_id=user.id,
                    token_hash="t" * 64,
                    csrf_token="csrf",
                    expires_at=now - timedelta(hours=2),
                )
            )
            db.add(
                RenderJob(
                    project_id=proj.id,
                    owner_user_id=user.id,
                    idempotency_key="cli-job-1",
                    idempotency_hash="k" * 64,
                    spec_json={},
                    status="succeeded",
                    progress_pct=100,
                    attempt_count=1,
                    output_object_key="outputs/cli-exp.mp4",
                    thumbnail_key=None,
                    expires_at=now - timedelta(minutes=10),
                    created_at=now - timedelta(days=1),
                    updated_at=now - timedelta(days=1),
                )
            )
            db.commit()
    finally:
        engine.dispose()

    # Invalid retention value returns exit code 1
    assert cli_main(["reap", "--render-retention-days", "0", "--database-url", db_url]) == 1
    assert "render_retention_days" in capsys.readouterr().err

    # Human-readable dry-run (without --json)
    rc_text = cli_main(["reap", "--dry-run", "--database-url", db_url])
    assert rc_text == 0
    text_out = capsys.readouterr().out
    assert "Reap complete (dry_run=True):" in text_out
    assert "render_jobs=1" in text_out

    rc_dry = cli_main(["reap", "--dry-run", "--json", "--database-url", db_url])
    assert rc_dry == 0
    dry_out = json.loads(capsys.readouterr().out)
    assert dry_out["dry_run"] is True
    assert dry_out["sessions_deleted"] == 1
    assert dry_out["render_jobs_deleted"] == 1
    assert dry_out["blobs_deleted"] == 1
    assert store.get_bytes("outputs/cli-exp.mp4") == b"video-bytes"

    rc_exec = cli_main(["reap", "--json", "--database-url", db_url])
    assert rc_exec == 0
    exec_out = json.loads(capsys.readouterr().out)
    assert exec_out["dry_run"] is False
    assert exec_out["sessions_deleted"] == 1
    assert exec_out["render_jobs_deleted"] == 1
    assert exec_out["blobs_deleted"] == 1
    with pytest.raises(FileNotFoundError):
        store.resolve_existing_path("outputs/cli-exp.mp4")


def test_reap_expired_deletes_chunk_manifest_blob(harness: AppHarness) -> None:
    user, proj = _seed_project(harness, "chunk-reap@example.com")
    now = datetime.now(UTC)
    exp_job_id = uuid.uuid4()
    manifest_key = f"outputs/manifests/{exp_job_id}.json"
    harness.store.put_bytes(manifest_key, b'{"job_id":"exp"}', content_type="application/json")

    with harness.session_factory() as db:
        db.add(
            RenderJob(
                id=exp_job_id,
                project_id=proj.id,
                owner_user_id=user.id,
                idempotency_key="exp-chunk-manifest",
                idempotency_hash="m" * 64,
                spec_json={},
                status="failed",
                progress_pct=50,
                attempt_count=3,
                is_chunked=True,
                output_object_key=None,
                thumbnail_key=None,
                expires_at=now - timedelta(hours=1),
                created_at=now - timedelta(days=2),
                updated_at=now - timedelta(days=2),
            )
        )
        db.commit()

        rep = reap_expired(db, harness.store, now=now, dry_run=False)
        assert rep.render_jobs_deleted == 1
        assert rep.blobs_deleted == 1

    with pytest.raises(FileNotFoundError):
        harness.store.resolve_existing_path(manifest_key)
