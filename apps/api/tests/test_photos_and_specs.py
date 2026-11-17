from __future__ import annotations

import io
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from PIL import ExifTags, Image, TiffImagePlugin
from sqlalchemy import inspect
from trip_api.db import create_db_engine
from trip_api.routers.photos import MAX_PHOTO_BYTES, MAX_PHOTOS_PER_PROJECT

if TYPE_CHECKING:
    from conftest import HarnessFactory


def _make_jpeg_with_exif(
    *,
    size: tuple[int, int] = (2000, 1000),
    date_str: str = "2001:07:01 10:30:00",
    offset_str: str = "+02:00",
    lat_dms: tuple[tuple[int, int], tuple[int, int], tuple[int, int]] = (
        (46, 1),
        (30, 1),
        (0, 1),
    ),
    lat_ref: str = "N",
    lon_dms: tuple[tuple[int, int], tuple[int, int], tuple[int, int]] = (
        (7, 1),
        (54, 1),
        (0, 1),
    ),
    lon_ref: str = "E",
    orientation: int = 6,
) -> bytes:
    img = Image.new("RGB", size, color=(20, 120, 200))
    exif = Image.Exif()
    exif[ExifTags.Base.Orientation] = orientation
    exif[ExifTags.Base.Software] = "SecretCameraFirmware"

    exif_ifd = exif.get_ifd(ExifTags.IFD.Exif)
    exif_ifd[36867] = date_str  # DateTimeOriginal
    exif_ifd[36881] = offset_str  # OffsetTimeOriginal

    gps_ifd = exif.get_ifd(ExifTags.IFD.GPSInfo)
    gps_ifd[1] = lat_ref
    gps_ifd[2] = tuple(TiffImagePlugin.IFDRational(n, d) for n, d in lat_dms)
    gps_ifd[3] = lon_ref
    gps_ifd[4] = tuple(TiffImagePlugin.IFDRational(n, d) for n, d in lon_dms)

    buf = io.BytesIO()
    img.save(buf, format="JPEG", exif=exif.tobytes(), quality=90)
    return buf.getvalue()


def test_alembic_migration_0003_project_photos(tmp_path: Path, monkeypatch: Any) -> None:
    db_file = tmp_path / "alembic_0003.db"
    db_url = f"sqlite:///{db_file}"
    monkeypatch.setenv("DATABASE_URL", db_url)

    api_dir = Path(__file__).resolve().parents[1]
    cfg = AlembicConfig(str(api_dir / "alembic.ini"))
    cfg.set_main_option("script_location", str(api_dir / "alembic"))
    cfg.set_main_option("sqlalchemy.url", db_url)

    alembic_command.upgrade(cfg, "head")
    engine = create_db_engine(db_url)
    try:
        insp = inspect(engine)
        assert "project_photos" in insp.get_table_names()
        cols = {c["name"] for c in insp.get_columns("project_photos")}
        assert {
            "id",
            "project_id",
            "stage_id",
            "source_type",
            "external_media_id",
            "object_key",
            "thumb_object_key",
            "width",
            "height",
            "byte_size",
            "caption",
            "taken_at_utc",
            "lat",
            "lon",
            "matched_lat",
            "matched_lon",
            "matched_progress",
            "match_method",
            "enabled",
            "created_at",
        } <= cols
    finally:
        engine.dispose()

    alembic_command.downgrade(cfg, "0002_stage_activity_share_audit")
    engine_down = create_db_engine(db_url)
    try:
        insp_down = inspect(engine_down)
        assert "project_photos" not in insp_down.get_table_names()
    finally:
        engine_down.dispose()

    alembic_command.upgrade(cfg, "head")


def test_public_config_google_photos_flag(make_harness: HarnessFactory) -> None:
    h = make_harness()
    client = h.login_client
    _ = client
    from fastapi.testclient import TestClient

    anon = TestClient(h.app)
    cfg_resp = anon.get("/api/v1/public-config")
    assert cfg_resp.status_code == 200
    assert cfg_resp.json()["google_photos_configured"] is False

    h_g = make_harness(google_client_id="gid", google_client_secret="gsec")
    anon_g = TestClient(h_g.app)
    assert anon_g.get("/api/v1/public-config").json()["google_photos_configured"] is True


def test_photo_polyglot_rejection_and_size_bounds(
    make_harness: HarnessFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = make_harness(rate_limit_photo_max=30)
    h.create_user("alice@example.com")
    alice, csrf = h.login_client("alice@example.com")

    proj_id = alice.post(
        "/api/v1/projects",
        json={"title": "Photo Security Trip"},
        headers={"X-CSRF-Token": csrf},
    ).json()["id"]

    valid_jpeg = _make_jpeg_with_exif(size=(100, 80))
    gif_buf = io.BytesIO()
    Image.new("RGB", (16, 16), color=(255, 0, 0)).save(gif_buf, format="GIF")

    bad_payloads = [
        (
            "evil.svg",
            b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
            "image/svg+xml",
        ),
        ("polyglot.jpg", b"<html><script>alert(1)</script></html>" + valid_jpeg, "image/jpeg"),
        ("anim.gif", gif_buf.getvalue(), "image/gif"),
        ("corrupt.jpg", b"\xff\xd8\xff\xe0not-a-valid-jpeg-stream", "image/jpeg"),
        ("empty.jpg", b"", "image/jpeg"),
    ]
    for fname, payload, ctype in bad_payloads:
        resp = alice.post(
            f"/api/v1/projects/{proj_id}/photos",
            files={"file": (fname, payload, ctype)},
            headers={"X-CSRF-Token": csrf},
        )
        assert resp.status_code == 422, (
            f"{fname} should be rejected with 422, got {resp.status_code}"
        )

    # Half-pair lat without lon -> 422
    half_pair = alice.post(
        f"/api/v1/projects/{proj_id}/photos",
        files={"file": ("ok.jpg", valid_jpeg, "image/jpeg")},
        data={"lat": "0.1"},
        headers={"X-CSRF-Token": csrf},
    )
    assert half_pair.status_code == 422

    # Decompression bomb protection -> 422
    monkeypatch.setattr("trip_api.routers.photos.MAX_PHOTO_PIXELS", 50)
    bomb_resp = alice.post(
        f"/api/v1/projects/{proj_id}/photos",
        files={"file": ("bomb.jpg", valid_jpeg, "image/jpeg")},
        headers={"X-CSRF-Token": csrf},
    )
    assert bomb_resp.status_code == 422

    # Oversized upload (> MAX_PHOTO_BYTES) -> 413
    monkeypatch.setattr("trip_api.routers.photos.MAX_PHOTO_BYTES", 100)
    oversized = alice.post(
        f"/api/v1/projects/{proj_id}/photos",
        files={"file": ("huge.jpg", valid_jpeg, "image/jpeg")},
        headers={"X-CSRF-Token": csrf},
    )
    assert oversized.status_code == 413
    assert MAX_PHOTO_BYTES == 15 * 1024 * 1024


def test_photo_exif_extraction_metadata_stripping_crud_and_project_cleanup(
    make_harness: HarnessFactory,
) -> None:
    h = make_harness(rate_limit_photo_max=60)
    h.create_user("alice@example.com")
    h.create_user("bob@example.com")
    alice, csrf = h.login_client("alice@example.com")
    bob, bob_csrf = h.login_client("bob@example.com")

    proj_id = alice.post(
        "/api/v1/projects",
        json={"title": "Fictional Photos"},
        headers={"X-CSRF-Token": csrf},
    ).json()["id"]

    # Update RenderSpecV2 with 180s duration + adaptive zoom + photo settings
    patch_spec = alice.patch(
        f"/api/v1/projects/{proj_id}",
        json={
            "revision": 1,
            "settings": {
                "duration_seconds": 180.0,
                "follow_zoom_mode": "adaptive",
                "follow_zoom_offset": 0.5,
                "target_screen_px_per_sec": 200.0,
                "show_photos": True,
                "photo_display_seconds": 4.0,
                "pause_on_photo": True,
                "statistics_panel": {"collision_avoidance": True},
            },
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert patch_spec.status_code == 200, patch_spec.text
    assert patch_spec.json()["settings"]["duration_seconds"] == 180.0
    assert patch_spec.json()["settings"]["pause_on_photo"] is True

    # JPEG with fictional EXIF GPS, OffsetTimeOriginal +02:00, Orientation=6
    jpeg_bytes = _make_jpeg_with_exif(
        size=(2000, 1000),
        date_str="2001:01:01 10:30:00",
        offset_str="+02:00",
        lat_dms=((0, 1), (6, 1), (0, 1)),
        lat_ref="N",
        lon_dms=((0, 1), (12, 1), (0, 1)),
        lon_ref="E",
        orientation=6,
    )
    # Missing CSRF -> 403
    assert (
        alice.post(
            f"/api/v1/projects/{proj_id}/photos",
            files={"file": ("pass.jpg", jpeg_bytes, "image/jpeg")},
        ).status_code
        == 403
    )

    up1 = alice.post(
        f"/api/v1/projects/{proj_id}/photos",
        files={"file": ("pass.jpg", jpeg_bytes, "image/jpeg")},
        data={"caption": "Top of the col"},
        headers={"X-CSRF-Token": csrf},
    )
    assert up1.status_code == 201, up1.text
    p1 = up1.json()
    assert p1["caption"] == "Top of the col"
    assert p1["lat"] == pytest.approx(0.1, abs=1e-5)
    assert p1["lon"] == pytest.approx(0.2, abs=1e-5)
    # 10:30:00 +02:00 -> 08:30:00Z
    assert datetime.fromisoformat(p1["taken_at_utc"].replace("Z", "+00:00")) == datetime(
        2001, 1, 1, 8, 30, 0, tzinfo=UTC
    )
    # Orientation 6 transposed 2000x1000 -> 1000x2000, downscaled to max 1400 -> 700x1400
    assert p1["width"] == 700
    assert p1["height"] == 1400
    assert p1["enabled"] is True

    # Verify stored full & thumb WebP blobs exist and have ZERO EXIF/metadata
    full_key = f"photos/{proj_id}/{p1['id']}.webp"
    thumb_key = f"photos/{proj_id}/{p1['id']}_thumb.webp"
    full_path = h.store.resolve_existing_path(full_key)
    thumb_path = h.store.resolve_existing_path(thumb_key)

    with Image.open(full_path) as full_img:
        assert full_img.format == "WEBP"
        assert full_img.size == (700, 1400)
        assert len(full_img.getexif()) == 0
        assert "exif" not in full_img.info
        assert "icc_profile" not in full_img.info
        assert "xmp" not in full_img.info

    with Image.open(thumb_path) as thumb_img:
        assert thumb_img.format == "WEBP"
        assert max(thumb_img.size) == 640
        assert len(thumb_img.getexif()) == 0

    # South/West hemisphere EXIF + RGBA PNG test
    sw_jpeg = _make_jpeg_with_exif(
        size=(200, 100),
        date_str="2001:01:02 15:00:00",
        offset_str="-03:00",
        lat_dms=((0, 1), (1, 1), (36, 1)),
        lat_ref="S",
        lon_dms=((1, 1), (12, 1), (0, 1)),
        lon_ref="W",
        orientation=1,
    )
    up2 = alice.post(
        f"/api/v1/projects/{proj_id}/photos",
        files={"file": ("sw.jpg", sw_jpeg, "image/jpeg")},
        headers={"X-CSRF-Token": csrf},
    )
    assert up2.status_code == 201
    p2 = up2.json()
    assert p2["lat"] == pytest.approx(-0.0267, abs=1e-4)
    assert p2["lon"] == pytest.approx(-1.2, abs=1e-4)

    png_buf = io.BytesIO()
    Image.new("RGBA", (120, 90), color=(0, 200, 100, 128)).save(png_buf, format="PNG")
    up3 = alice.post(
        f"/api/v1/projects/{proj_id}/photos",
        files={"file": ("alpha.png", png_buf.getvalue(), "image/png")},
        data={"lat": "0.11", "lon": "0.21", "caption": "Alpha stop"},
        headers={"X-CSRF-Token": csrf},
    )
    assert up3.status_code == 201
    p3 = up3.json()
    assert p3["match_method"] == "manual"

    # Authenticated image stream (full & thumb)
    img_resp = alice.get(f"/api/v1/projects/{proj_id}/photos/{p1['id']}/image?variant=thumb")
    assert img_resp.status_code == 200
    assert img_resp.headers["content-type"] == "image/webp"
    assert img_resp.headers["x-content-type-options"] == "nosniff"
    assert img_resp.content[:4] == b"RIFF"

    # BOLA checks for Bob
    assert bob.get(f"/api/v1/projects/{proj_id}/photos").status_code == 404
    assert bob.get(f"/api/v1/projects/{proj_id}/photos/{p1['id']}/image").status_code == 404
    assert (
        bob.patch(
            f"/api/v1/projects/{proj_id}/photos/{p1['id']}",
            json={"caption": "hacked"},
            headers={"X-CSRF-Token": bob_csrf},
        ).status_code
        == 404
    )
    assert (
        bob.delete(
            f"/api/v1/projects/{proj_id}/photos/{p1['id']}",
            headers={"X-CSRF-Token": bob_csrf},
        ).status_code
        == 404
    )

    # PATCH update & toggle
    patched = alice.patch(
        f"/api/v1/projects/{proj_id}/photos/{p2['id']}",
        json={"caption": "Updated SW", "enabled": False},
        headers={"X-CSRF-Token": csrf},
    )
    assert patched.status_code == 200
    assert patched.json()["caption"] == "Updated SW"
    assert patched.json()["enabled"] is False

    # Enable sharing & verify public share redaction + image endpoint
    share_resp = alice.post(
        f"/api/v1/projects/{proj_id}/share",
        headers={"X-CSRF-Token": csrf},
    )
    assert share_resp.status_code == 200
    slug = share_resp.json()["share_slug"]

    from fastapi.testclient import TestClient

    anon = TestClient(h.app)
    pub = anon.get(f"/api/v1/share/{slug}")
    assert pub.status_code == 200
    pub_photos = pub.json()["photos"]
    # p2 was toggled enabled=False so only p1 and p3 are exposed
    pub_ids = {ph["id"] for ph in pub_photos}
    assert pub_ids == {p1["id"], p3["id"]}
    for ph in pub_photos:
        for forbidden in (
            "object_key",
            "thumb_object_key",
            "external_media_id",
            "source_type",
            "byte_size",
            "project_id",
        ):
            assert forbidden not in ph

    assert (
        anon.get(f"/api/v1/share/{slug}/photos/{p1['id']}/image?variant=thumb").status_code == 200
    )
    assert anon.get(f"/api/v1/share/{slug}/photos/{p2['id']}/image").status_code == 404

    # Delete single photo deletes DB row + both blobs
    del_p3 = alice.delete(
        f"/api/v1/projects/{proj_id}/photos/{p3['id']}",
        headers={"X-CSRF-Token": csrf},
    )
    assert del_p3.status_code == 204
    with pytest.raises(FileNotFoundError):
        h.store.resolve_existing_path(f"photos/{proj_id}/{p3['id']}.webp")
    with pytest.raises(FileNotFoundError):
        h.store.resolve_existing_path(f"photos/{proj_id}/{p3['id']}_thumb.webp")

    # Delete project cleans up remaining photo blobs (p1 and p2)
    del_proj = alice.delete(
        f"/api/v1/projects/{proj_id}",
        headers={"X-CSRF-Token": csrf},
    )
    assert del_proj.status_code == 204
    with pytest.raises(FileNotFoundError):
        h.store.resolve_existing_path(full_key)
    with pytest.raises(FileNotFoundError):
        h.store.resolve_existing_path(thumb_key)


def test_photo_project_limit_and_rate_limit(
    make_harness: HarnessFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = make_harness(rate_limit_photo_max=5, rate_limit_photo_window_s=60)
    h.create_user("alice@example.com")
    alice, csrf = h.login_client("alice@example.com")

    proj_id = alice.post(
        "/api/v1/projects",
        json={"title": "Limits Trip"},
        headers={"X-CSRF-Token": csrf},
    ).json()["id"]

    small_jpg = _make_jpeg_with_exif(size=(32, 32))
    monkeypatch.setattr("trip_api.routers.photos.MAX_PHOTOS_PER_PROJECT", 2)

    for i in range(2):
        r = alice.post(
            f"/api/v1/projects/{proj_id}/photos",
            files={"file": (f"p{i}.jpg", small_jpg, "image/jpeg")},
            headers={"X-CSRF-Token": csrf},
        )
        assert r.status_code == 201

    # 3rd exceeds project cap -> 422
    cap_resp = alice.post(
        f"/api/v1/projects/{proj_id}/photos",
        files={"file": ("p3.jpg", small_jpg, "image/jpeg")},
        headers={"X-CSRF-Token": csrf},
    )
    assert cap_resp.status_code == 422

    # Raise project cap and hit rate limit (limit=5; 3 requests already counted above)
    monkeypatch.setattr("trip_api.routers.photos.MAX_PHOTOS_PER_PROJECT", 50)
    for i in range(2):
        r = alice.post(
            f"/api/v1/projects/{proj_id}/photos",
            files={"file": (f"r{i}.jpg", small_jpg, "image/jpeg")},
            headers={"X-CSRF-Token": csrf},
        )
        assert r.status_code == 201

    rl_resp = alice.post(
        f"/api/v1/projects/{proj_id}/photos",
        files={"file": ("r_over.jpg", small_jpg, "image/jpeg")},
        headers={"X-CSRF-Token": csrf},
    )
    assert rl_resp.status_code == 429
    assert MAX_PHOTOS_PER_PROJECT == 50
    assert uuid.UUID(proj_id)
