from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from trip_api.config import Settings

if TYPE_CHECKING:
    from conftest import HarnessFactory


def _create_project(client: TestClient, csrf: str) -> str:
    response = client.post(
        "/api/v1/projects",
        json={"title": "Fictional Marked Trails"},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


def test_marked_trail_types_default_off_and_round_trip_through_project_settings(
    make_harness: HarnessFactory,
) -> None:
    h = make_harness()
    h.create_user("trail-author@example.com")
    client, csrf = h.login_client("trail-author@example.com")
    project_id = _create_project(client, csrf)

    detail = client.get(f"/api/v1/projects/{project_id}").json()
    assert detail["settings"]["marked_trail_types"] == []

    patch = client.patch(
        f"/api/v1/projects/{project_id}",
        json={"revision": detail["revision"], "settings": {"marked_trail_types": ["hiking"]}},
        headers={"X-CSRF-Token": csrf},
    )
    assert patch.status_code == 200, patch.text
    assert patch.json()["settings"]["marked_trail_types"] == ["hiking"]

    reloaded = client.get(f"/api/v1/projects/{project_id}").json()
    assert reloaded["settings"]["marked_trail_types"] == ["hiking"]


def test_unknown_marked_trail_type_is_rejected_and_stored_value_is_kept(
    make_harness: HarnessFactory,
) -> None:
    h = make_harness()
    h.create_user("trail-reject@example.com")
    client, csrf = h.login_client("trail-reject@example.com")
    project_id = _create_project(client, csrf)
    revision = client.get(f"/api/v1/projects/{project_id}").json()["revision"]

    for bad_value in (["skiing"], ["hiking", "trail"], "hiking", ["hiking", "hiking"]):
        response = client.patch(
            f"/api/v1/projects/{project_id}",
            json={"revision": revision, "settings": {"marked_trail_types": bad_value}},
            headers={"X-CSRF-Token": csrf},
        )
        assert response.status_code == 422, (bad_value, response.text)

    detail = client.get(f"/api/v1/projects/{project_id}").json()
    assert detail["settings"]["marked_trail_types"] == []


def test_public_config_exposes_marked_trails_base_url_without_trailing_slash(
    make_harness: HarnessFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MARKED_TRAILS_BASE_URL", raising=False)
    default_config = TestClient(make_harness().app).get("/api/v1/public-config").json()
    assert default_config["marked_trails_base_url"] == ""

    configured = make_harness(marked_trails_base_url="https://trails.example.test/base/")
    config = TestClient(configured.app).get("/api/v1/public-config").json()
    assert config["marked_trails_base_url"] == "https://trails.example.test/base"

    local = make_harness(marked_trails_base_url="/tiles/marked-trails")
    config = TestClient(local.app).get("/api/v1/public-config").json()
    assert config["marked_trails_base_url"] == "/tiles/marked-trails"


@pytest.mark.parametrize(
    "value",
    [
        "tiles/marked-trails",
        "//trails.example.test/base",
        "http://trails.example.test/base",
        "https://",
        "javascript:alert(1)",
        "/tiles/../api",
        "/tiles/%2e%2e/api",
        "/tiles/%2E%2e/api",
        "/\\trails.example.test/base",
        "https://trails.example.test/base?x=1",
    ],
)
def test_marked_trails_base_url_must_be_root_relative_or_https(value: str) -> None:
    with pytest.raises(ValidationError, match="marked_trails_base_url"):
        Settings(_env_file=None, marked_trails_base_url=value)
