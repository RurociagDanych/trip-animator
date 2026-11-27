from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi.testclient import TestClient
from sqlalchemy import select
from trip_api.cli import main as cli_main
from trip_api.db import CreditLedgerEntry, CreditVoucher, RenderJob
from trip_api.remote_renders import (
    RenderCallbackPayload,
    handle_render_callback,
    process_remote_renders_once,
)
from trip_api.render_backends import RemoteExecutionRef, RemoteExecutionStatus, RemoteSubmitRequest

if TYPE_CHECKING:
    from conftest import HarnessFactory


def _create_project_with_stage(
    client: TestClient,
    csrf: str,
    sample_gpx: bytes,
) -> str:
    proj_resp = client.post(
        "/api/v1/projects",
        headers={"X-CSRF-Token": csrf},
        json={"title": "Credits Test Trip"},
    )
    assert proj_resp.status_code == 201, proj_resp.text
    project_id = str(proj_resp.json()["id"])

    stage_resp = client.post(
        f"/api/v1/projects/{project_id}/gpx",
        headers={"X-CSRF-Token": csrf},
        files={"file": ("stage.gpx", sample_gpx, "application/gpx+xml")},
        data={"name": "Day 1", "day_index": "1"},
    )
    assert stage_resp.status_code == 201, stage_resp.text
    return project_id


def _spec_payload(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "resolution": "1920x1080",
        "fps": 30,
        "duration_seconds": 60.0,
        "intro_hold_seconds": 0.0,
        "outro_hold_seconds": 0.0,
        "terrain_3d": False,
        "show_photos": False,
    }
    base.update(overrides)
    return base


def test_new_user_starts_at_zero_credits_and_402_on_server_render(
    make_harness: HarnessFactory,
    sample_gpx: bytes,
) -> None:
    harness = make_harness()
    harness.create_user("zero@example.com", initial_credits=0)
    client, csrf = harness.login_client("zero@example.com")
    try:
        credits_resp = client.get("/api/v1/credits")
        assert credits_resp.status_code == 200
        assert credits_resp.json() == {"balance": 0, "ledger": []}

        project_id = _create_project_with_stage(client, csrf, sample_gpx)

        quote_resp = client.post(
            f"/api/v1/projects/{project_id}/renders/quote",
            json=_spec_payload(),
        )
        assert quote_resp.status_code == 200
        quote = quote_resp.json()
        assert quote["credits"] == 100
        assert quote["balance"] == 0
        assert quote["sufficient"] is False
        assert quote["cached"] is False

        render_resp = client.post(
            f"/api/v1/projects/{project_id}/renders",
            headers={"X-CSRF-Token": csrf, "Idempotency-Key": "idem-zero-001"},
            json=_spec_payload(),
        )
        assert render_resp.status_code == 402
        assert "credits" in render_resp.json()["detail"].lower()
    finally:
        client.close()


def test_voucher_cli_lifecycle_redeem_single_and_multi_use_and_rate_limit(
    make_harness: HarnessFactory,
    capsys: Any,
) -> None:
    harness = make_harness()
    harness.create_user("u1@example.com", initial_credits=0)
    harness.create_user("u2@example.com", initial_credits=0)
    harness.create_user("u3@example.com", initial_credits=0)

    db_url = harness.settings.database_url

    rc = cli_main(
        [
            "create-voucher",
            "--credits",
            "250",
            "--max-redemptions",
            "2",
            "--code",
            "BETA-2026-ALPHA",
            "--note",
            "Beta wave 1",
            "--database-url",
            db_url,
            "--json",
        ]
    )
    assert rc == 0
    created = json.loads(capsys.readouterr().out)
    assert created["code"] == "BETA-2026-ALPHA"
    assert created["credits_amount"] == 250
    assert created["max_redemptions"] == 2

    rc_list = cli_main(["list-vouchers", "--database-url", db_url, "--json"])
    assert rc_list == 0
    listed = json.loads(capsys.readouterr().out)
    assert len(listed) == 1
    assert listed[0]["id"] == created["id"]
    assert "BETA-2026-ALPHA" not in json.dumps(listed)

    c1, csrf1 = harness.login_client("u1@example.com")
    c2, csrf2 = harness.login_client("u2@example.com")
    c3, csrf3 = harness.login_client("u3@example.com")
    try:
        r1 = c1.post(
            "/api/v1/credits/redeem",
            headers={"X-CSRF-Token": csrf1},
            json={"code": "  beta-2026-alpha  "},
        )
        assert r1.status_code == 200, r1.text
        assert r1.json()["credits_granted"] == 250
        assert r1.json()["balance"] == 250

        dup = c1.post(
            "/api/v1/credits/redeem",
            headers={"X-CSRF-Token": csrf1},
            json={"code": "BETA-2026-ALPHA"},
        )
        assert dup.status_code == 409

        r2 = c2.post(
            "/api/v1/credits/redeem",
            headers={"X-CSRF-Token": csrf2},
            json={"code": "BETA-2026-ALPHA"},
        )
        assert r2.status_code == 200
        assert r2.json()["balance"] == 250

        r3 = c3.post(
            "/api/v1/credits/redeem",
            headers={"X-CSRF-Token": csrf3},
            json={"code": "BETA-2026-ALPHA"},
        )
        assert r3.status_code == 400

        rc_v2 = cli_main(
            [
                "create-voucher",
                "--credits",
                "100",
                "--code",
                "REVOKE-ME-NOW",
                "--database-url",
                db_url,
                "--json",
            ]
        )
        assert rc_v2 == 0
        v2 = json.loads(capsys.readouterr().out)

        rc_rev = cli_main(["revoke-voucher", "--id", v2["id"], "--database-url", db_url, "--json"])
        assert rc_rev == 0
        _ = capsys.readouterr()

        revoked_attempt = c3.post(
            "/api/v1/credits/redeem",
            headers={"X-CSRF-Token": csrf3},
            json={"code": "REVOKE-ME-NOW"},
        )
        assert revoked_attempt.status_code == 400

        rc_grant = cli_main(
            [
                "grant-credits",
                "--email",
                "u3@example.com",
                "--delta",
                "75",
                "--note",
                "Manual test grant",
                "--database-url",
                db_url,
                "--json",
            ]
        )
        assert rc_grant == 0
        grant_out = json.loads(capsys.readouterr().out)
        assert grant_out["balance"] == 75
        assert c3.get("/api/v1/credits").json()["balance"] == 75

        no_csrf = c3.post("/api/v1/credits/redeem", json={"code": "BETA-2026-ALPHA"})
        assert no_csrf.status_code == 403

        rc_short = cli_main(
            [
                "create-voucher",
                "--credits",
                "50",
                "--code",
                "BETA26",
                "--expires-in-days",
                "1",
                "--database-url",
                db_url,
                "--json",
            ]
        )
        assert rc_short == 0
        short_v = json.loads(capsys.readouterr().out)
        assert len(short_v["code_prefix"]) < len("BETA26")

        with harness.session_factory() as db:
            v_row = db.get(CreditVoucher, uuid.UUID(short_v["id"]))
            assert v_row is not None
            v_row.expires_at = datetime.now(UTC) - timedelta(minutes=5)
            db.commit()

        expired_attempt = c3.post(
            "/api/v1/credits/redeem",
            headers={"X-CSRF-Token": csrf3},
            json={"code": "BETA26"},
        )
        assert expired_attempt.status_code == 400
        assert "expired" in expired_attempt.json()["detail"].lower()

        last_status = 0
        for _ in range(12):
            resp = c3.post(
                "/api/v1/credits/redeem",
                headers={"X-CSRF-Token": csrf3},
                json={"code": "NONEXISTENT-CODE"},
            )
            last_status = resp.status_code
            if last_status == 429:
                break
        assert last_status == 429
    finally:
        c1.close()
        c2.close()
        c3.close()


def test_debit_idempotency_cache_hit_zero_cost_and_cancel_refund_rules(
    make_harness: HarnessFactory,
    sample_gpx: bytes,
) -> None:
    harness = make_harness()
    harness.create_user("render-user@example.com", initial_credits=150)
    client, csrf = harness.login_client("render-user@example.com")
    try:
        project_id = _create_project_with_stage(client, csrf, sample_gpx)

        r1 = client.post(
            f"/api/v1/projects/{project_id}/renders",
            headers={"X-CSRF-Token": csrf, "Idempotency-Key": "idem-debit-001"},
            json=_spec_payload(),
        )
        assert r1.status_code == 202, r1.text
        job1_id = r1.json()["id"]
        assert r1.json()["credits_charged"] == 100
        assert client.get("/api/v1/credits").json()["balance"] == 50

        r1_dup = client.post(
            f"/api/v1/projects/{project_id}/renders",
            headers={"X-CSRF-Token": csrf, "Idempotency-Key": "idem-debit-001"},
            json=_spec_payload(),
        )
        assert r1_dup.status_code == 202
        assert r1_dup.json()["id"] == job1_id
        assert client.get("/api/v1/credits").json()["balance"] == 50

        cancel_queued = client.post(
            f"/api/v1/renders/{job1_id}/cancel",
            headers={"X-CSRF-Token": csrf},
        )
        assert cancel_queued.status_code == 200
        assert client.get("/api/v1/credits").json()["balance"] == 150

        r2 = client.post(
            f"/api/v1/projects/{project_id}/renders",
            headers={"X-CSRF-Token": csrf, "Idempotency-Key": "idem-debit-002"},
            json=_spec_payload(),
        )
        assert r2.status_code == 202
        job2_id = r2.json()["id"]
        assert client.get("/api/v1/credits").json()["balance"] == 50

        with harness.session_factory() as db:
            job2 = db.get(RenderJob, uuid.UUID(job2_id))
            assert job2 is not None
            job2.status = "running"
            db.commit()

        cancel_running = client.post(
            f"/api/v1/renders/{job2_id}/cancel",
            headers={"X-CSRF-Token": csrf},
        )
        assert cancel_running.status_code == 200
        assert client.get("/api/v1/credits").json()["balance"] == 50

        with harness.session_factory() as db:
            job2 = db.get(RenderJob, uuid.UUID(job2_id))
            assert job2 is not None
            out_key = f"outputs/{job2.id}.mp4"
            harness.store.put_bytes(out_key, b"fake-mp4-bytes")
            job2.status = "succeeded"
            job2.progress_pct = 100
            job2.output_object_key = out_key
            job2.expires_at = datetime.now(UTC) + timedelta(days=7)
            db.commit()

        quote_cached = client.post(
            f"/api/v1/projects/{project_id}/renders/quote",
            json=_spec_payload(),
        )
        assert quote_cached.status_code == 200
        assert quote_cached.json()["cached"] is True
        assert quote_cached.json()["credits"] == 0
        assert quote_cached.json()["sufficient"] is True

        r_cached = client.post(
            f"/api/v1/projects/{project_id}/renders",
            headers={"X-CSRF-Token": csrf, "Idempotency-Key": "idem-cached-003"},
            json=_spec_payload(),
        )
        assert r_cached.status_code == 202
        assert r_cached.json()["status"] == "succeeded"
        assert r_cached.json()["credits_charged"] == 0
        assert client.get("/api/v1/credits").json()["balance"] == 50
    finally:
        client.close()


def test_remote_render_failure_and_cancel_before_vs_after_dispatch_refunds(
    make_harness: HarnessFactory,
    sample_gpx: bytes,
) -> None:
    harness = make_harness(render_backend="aws_sfn_lambda", render_remote_max_attempts=1)
    harness.create_user("remote-credits@example.com", initial_credits=300)

    class _FakeBackend:
        name = "aws_sfn_lambda"
        is_remote = True

        def __init__(self) -> None:
            self.fail_submit = False
            self.cancelled: list[str] = []

        def submit(self, request: RemoteSubmitRequest) -> RemoteExecutionRef:
            if self.fail_submit:
                raise RuntimeError("AWS throttling")
            return RemoteExecutionRef(
                execution_id=f"arn:test:{request.job_id}",
                staging_prefix=f"staging/{request.job_id}/",
            )

        def get_status(self, execution_ref: str) -> RemoteExecutionStatus:
            _ = execution_ref
            return RemoteExecutionStatus(state="running", progress_pct=50)

        def cancel(self, execution_ref: str) -> None:
            self.cancelled.append(execution_ref)

        def download_output(self, execution_ref: str, key: str, dest: Path) -> None:
            _ = (execution_ref, key, dest)

        def cleanup_staging(self, job_id: uuid.UUID) -> None:
            _ = job_id

    backend = _FakeBackend()
    harness.app.state.render_backend = backend

    client, csrf = harness.login_client("remote-credits@example.com")
    try:
        project_id = _create_project_with_stage(client, csrf, sample_gpx)

        r_pre = client.post(
            f"/api/v1/projects/{project_id}/renders",
            headers={"X-CSRF-Token": csrf, "Idempotency-Key": "idem-rem-pre-1"},
            json=_spec_payload(),
        )
        assert r_pre.status_code == 202
        assert client.get("/api/v1/credits").json()["balance"] == 200
        assert (
            client.post(
                f"/api/v1/renders/{r_pre.json()['id']}/cancel",
                headers={"X-CSRF-Token": csrf},
            ).status_code
            == 200
        )
        assert client.get("/api/v1/credits").json()["balance"] == 300

        r_fail = client.post(
            f"/api/v1/projects/{project_id}/renders",
            headers={"X-CSRF-Token": csrf, "Idempotency-Key": "idem-rem-fail-2"},
            json=_spec_payload(),
        )
        assert r_fail.status_code == 202
        fail_job_id = uuid.UUID(r_fail.json()["id"])
        assert client.get("/api/v1/credits").json()["balance"] == 200

        process_remote_renders_once(
            harness.session_factory,
            harness.store,
            harness.settings,
            backend,
        )
        handle_render_callback(
            harness.session_factory,
            harness.store,
            harness.settings,
            backend,
            RenderCallbackPayload(
                job_id=fail_job_id,
                event="failed",
                error_message="Lambda OOM",
            ),
        )
        assert client.get("/api/v1/credits").json()["balance"] == 300

        handle_render_callback(
            harness.session_factory,
            harness.store,
            harness.settings,
            backend,
            RenderCallbackPayload(
                job_id=fail_job_id,
                event="failed",
                error_message="Duplicate callback",
            ),
        )
        assert client.get("/api/v1/credits").json()["balance"] == 300

        r_post = client.post(
            f"/api/v1/projects/{project_id}/renders",
            headers={"X-CSRF-Token": csrf, "Idempotency-Key": "idem-rem-post-3"},
            json=_spec_payload(),
        )
        assert r_post.status_code == 202
        post_job_id = r_post.json()["id"]
        assert client.get("/api/v1/credits").json()["balance"] == 200

        process_remote_renders_once(
            harness.session_factory,
            harness.store,
            harness.settings,
            backend,
        )
        assert (
            client.post(
                f"/api/v1/renders/{post_job_id}/cancel",
                headers={"X-CSRF-Token": csrf},
            ).status_code
            == 200
        )
        assert client.get("/api/v1/credits").json()["balance"] == 200
        assert backend.cancelled == [f"arn:test:{post_job_id}"]

        backend.fail_submit = True
        r_exhaust = client.post(
            f"/api/v1/projects/{project_id}/renders",
            headers={"X-CSRF-Token": csrf, "Idempotency-Key": "idem-test-0004"},
            json=_spec_payload(),
        )
        assert r_exhaust.status_code == 202
        assert client.get("/api/v1/credits").json()["balance"] == 100

        process_remote_renders_once(
            harness.session_factory,
            harness.store,
            harness.settings,
            backend,
        )
        assert client.get("/api/v1/credits").json()["balance"] == 200
    finally:
        client.close()

    with harness.session_factory() as db:
        refunds = list(
            db.scalars(
                select(CreditLedgerEntry).where(CreditLedgerEntry.entry_type == "render_refund")
            ).all()
        )
        assert len(refunds) == 3
        vouchers = list(db.scalars(select(CreditVoucher)).all())
        assert vouchers == []
