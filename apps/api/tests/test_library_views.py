"""Behavioral coverage for private rule-based library views."""

from datetime import UTC, datetime
from uuid import uuid4

from trip_api.routers import library_views


def test_saved_view_persists_rules_and_resolves_relative_dates(harness, monkeypatch):
    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 11, 21, 23, 30, tzinfo=UTC).astimezone(tz)

    monkeypatch.setattr(library_views, "datetime", _FrozenDatetime)
    harness.create_user("views@example.test")
    client, csrf = harness.login_client("views@example.test")
    rules = {
        "version": 1,
        "filters": {"sport": "cycling", "relative_days": 7},
        "sort": "distance_m",
        "direction": "asc",
    }
    created = client.post(
        "/api/v1/vault/views",
        json={"name": "Recent rides", "rules": rules},
        headers={"X-CSRF-Token": csrf},
    )
    assert created.status_code == 201, created.text
    view_id = created.json()["id"]
    assert created.json()["rules"] == rules
    assert client.get("/api/v1/vault/views").json()["items"][0]["id"] == view_id
    opened = client.get(f"/api/v1/vault/views/{view_id}")
    assert opened.status_code == 200
    assert opened.json()["resolved_filters"] == {
        "sport": "cycling",
        "start_date": "2026-11-15",
        "end_date": "2026-11-21",
    }
    assert opened.json()["rules"] == rules


def test_views_are_owner_scoped_and_writes_require_csrf(harness):
    harness.create_user("owner@example.test")
    harness.create_user("other@example.test")
    owner, csrf = harness.login_client("owner@example.test")
    other, other_csrf = harness.login_client("other@example.test")
    payload = {"name": "Private", "rules": {"version": 1}}
    assert owner.post("/api/v1/vault/views", json=payload).status_code == 403
    created = owner.post("/api/v1/vault/views", json=payload, headers={"X-CSRF-Token": csrf})
    assert created.status_code == 201, created.text
    view_id = created.json()["id"]
    assert other.get("/api/v1/vault/views").json() == {"items": []}
    assert other.get(f"/api/v1/vault/views/{view_id}").status_code == 404
    assert (
        other.delete(
            f"/api/v1/vault/views/{view_id}", headers={"X-CSRF-Token": other_csrf}
        ).status_code
        == 404
    )
    assert (
        owner.delete(f"/api/v1/vault/views/{view_id}", headers={"X-CSRF-Token": csrf}).status_code
        == 204
    )
    assert owner.get(f"/api/v1/vault/views/{view_id}").status_code == 404
    assert owner.get(f"/api/v1/vault/views/{uuid4()}").status_code == 404


def test_saved_view_validates_rules_and_keeps_fixed_dates(harness):
    harness.create_user("validation@example.test")
    client, csrf = harness.login_client("validation@example.test")
    headers = {"X-CSRF-Token": csrf}
    for rules in (
        {"version": 2},
        {"version": 1, "activity_ids": []},
        {"version": 1, "filters": {"relative_days": 0}},
        {"version": 1, "filters": {"start_date": "2020-01-02", "end_date": "2020-01-01"}},
        {"version": 1, "filters": {"min_distance_m": -1}},
        {"version": 1, "sort": "unsafe"},
        {"version": 1, "filters": {"relative_days": 7, "start_date": "2020-01-01"}},
    ):
        assert (
            client.post(
                "/api/v1/vault/views", json={"name": "Invalid", "rules": rules}, headers=headers
            ).status_code
            == 422
        )
    rules = {
        "version": 1,
        "filters": {"start_date": "2020-01-01", "end_date": "2020-02-01"},
        "sort": "started_at",
        "direction": "desc",
    }
    created = client.post(
        "/api/v1/vault/views", json={"name": "Fixed dates", "rules": rules}, headers=headers
    )
    assert created.status_code == 201, created.text
    opened = client.get(f"/api/v1/vault/views/{created.json()['id']}").json()
    assert opened["resolved_filters"] == rules["filters"]
