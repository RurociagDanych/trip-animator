from __future__ import annotations

from datetime import UTC, datetime, timedelta

from trip_api.db import OAuthConnection, User, VaultActivity, VaultSyncState
from trip_api.library_enrichment import enqueue_library_enrichment
from trip_api.security import hash_password


def _user(db, email: str, *, active: bool = True) -> User:
    user = User(
        email=email,
        display_name=email.split("@", 1)[0],
        password_hash=hash_password("correct-horse-battery-staple"),
        is_active=active,
        email_verified_at=datetime.now(UTC),
    )
    db.add(user)
    db.flush()
    return user


def _connected(db, user: User, athlete_id: str) -> None:
    db.add(
        OAuthConnection(
            user_id=user.id,
            provider="strava",
            external_athlete_id=athlete_id,
            access_token_enc="access",
            refresh_token_enc="refresh",
            expires_at=datetime.now(UTC) + timedelta(days=1),
            scope="activity:read_all",
        )
    )


def _activity(db, user: User, activity_id: int, **overrides) -> VaultActivity:
    values = {
        "user_id": user.id,
        "source_provider": "strava",
        "external_activity_id": str(activity_id),
        "strava_activity_id": activity_id,
        "name": "Synthetic ride",
        "started_at": datetime(2025, 1, 1, tzinfo=UTC),
        "local_date": datetime(2025, 1, 1).date(),
        "has_gps": True,
    }
    values.update(overrides)
    activity = VaultActivity(**values)
    db.add(activity)
    db.flush()
    return activity


def test_enqueues_eligible_activity_once_with_canonical_fences(harness):
    from trip_api.db import IngestJob

    with harness.session_factory() as db:
        user = _user(db, "owner@example.test")
        _connected(db, user, "athlete-1")
        activity = _activity(db, user, 1234)
        db.commit()
        assert enqueue_library_enrichment(db, batch_size=5) == 1
        assert enqueue_library_enrichment(db, batch_size=5) == 0
        job = db.query(IngestJob).one()
        assert job.status == "queued"
        assert job.external_object_id == 1234
        assert job.external_owner_id == "athlete-1"
        assert job.payload_json == {
            "kind": "library_enrichment",
            "owner_user_id": str(user.id),
            "catalog_activity_id": str(activity.id),
        }


def test_excludes_missing_gps_inactive_and_rate_paused_activities(harness):
    with harness.session_factory() as db:
        eligible_owner = _user(db, "eligible@example.test")
        _connected(db, eligible_owner, "athlete-2")
        _activity(db, eligible_owner, 21)
        _activity(db, eligible_owner, 22, has_gps=False)
        inactive = _user(db, "inactive@example.test", active=False)
        _connected(db, inactive, "athlete-3")
        _activity(db, inactive, 23)
        paused = _user(db, "paused@example.test")
        _connected(db, paused, "athlete-4")
        _activity(db, paused, 24)
        db.add(
            VaultSyncState(
                user_id=paused.id,
                provider="strava",
                status="rate_limited",
                next_retry_at=datetime.now(UTC) + timedelta(hours=1),
            )
        )
        db.commit()
        assert enqueue_library_enrichment(db, batch_size=10) == 1


def test_does_not_reset_permanent_failure_and_fills_bounded_batch(harness):
    from trip_api.db import IngestJob
    from trip_api.library_enrichment import _job_id

    with harness.session_factory() as db:
        user = _user(db, "retry@example.test")
        _connected(db, user, "athlete-5")
        failed_activity = _activity(db, user, 31)
        _activity(db, user, 32)
        failed_job = IngestJob(
            id=_job_id(failed_activity.id),
            provider="strava",
            event_type="update",
            external_owner_id="athlete-5",
            external_object_id=31,
            payload_json={
                "kind": "library_enrichment",
                "owner_user_id": str(user.id),
                "catalog_activity_id": str(failed_activity.id),
            },
            status="failed",
            attempt_count=5,
            error_message="permanent provider failure",
        )
        db.add(failed_job)
        db.commit()

        assert enqueue_library_enrichment(db, batch_size=1) == 1
        assert enqueue_library_enrichment(db, batch_size=1) == 0
        jobs = db.query(IngestJob).order_by(IngestJob.external_object_id).all()
        assert len(jobs) == 2
        failed = next(job for job in jobs if job.external_object_id == 31)
        queued = next(job for job in jobs if job.external_object_id == 32)
        assert failed.id == _job_id(failed_activity.id)
        assert failed.status == "failed"
        assert failed.attempt_count == 5
        assert failed.error_message == "permanent provider failure"
        assert queued.status == "queued"
