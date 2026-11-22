"""Project photo upload, hardened Pillow WebP sanitization, EXIF alignment, and pin routes."""

from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import math
import re
import uuid
import warnings
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from typing import Annotated, Any, Literal

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, Response, UploadFile
from PIL import ExifTags, Image, ImageOps, UnidentifiedImageError
from sqlalchemy import func, select
from trip_telemetry import (
    PhotoAlignmentInput,
    StageTrackInput,
    snap_photo_pin,
    solve_camera_clock_skew,
)

from trip_api.db import Project, ProjectPhoto
from trip_api.deps import AppSettings, CsrfAuth, CurrentAuth, DbSession, StoreDep, ensure_utc
from trip_api.rate_limit import check_rate_limit
from trip_api.schemas import (
    PhotoPinRequest,
    PhotoShiftClockOut,
    PhotoShiftClockRequest,
    ProjectPhotoOut,
    ProjectPhotoUpdateRequest,
)
from trip_api.services import (
    align_project_photos,
    get_owned_project,
    get_project_photos,
    get_project_stages,
    materialize_silver_parquets,
    photo_to_out,
    write_audit_event,
)

MAX_PHOTO_BYTES = 15 * 1024 * 1024  # 15 MiB (safely under Caddy 25 MiB cap)
MAX_PHOTOS_PER_PROJECT = 100
MAX_PHOTO_PIXELS = 40_000_000
FULL_MAX_DIM = 1400
THUMB_MAX_DIM = 640
_CHUNK_BYTES = 64 * 1024
_MULTIPART_OVERHEAD_BYTES = 1024 * 1024
_ALLOWED_PILLOW_FORMATS = ["JPEG", "PNG", "WEBP"]

Image.MAX_IMAGE_PIXELS = MAX_PHOTO_PIXELS

_OFFSET_RE = re.compile(r"^([+-])(\d{2}):?(\d{2})$")

router = APIRouter(prefix="/api/v1/projects/{project_id}/photos", tags=["photos"])
logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ProcessedPhoto:
    """Sanitized WebP payloads and extracted EXIF metadata."""

    full_bytes: bytes
    thumb_bytes: bytes
    width: int
    height: int
    taken_at_utc: datetime | None
    lat: float | None
    lon: float | None
    camera_make_model: str | None = None


def _has_allowed_image_magic(data: bytes) -> bool:
    if data.startswith(b"\xff\xd8\xff"):
        return True
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return True
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return True
    return False


def _rational_to_float(value: Any) -> float | None:
    try:
        if isinstance(value, tuple) and len(value) == 2:
            num = float(value[0])
            den = float(value[1])
            if den == 0.0:
                return None
            out = num / den
        else:
            out = float(value)
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    return out if math.isfinite(out) else None


def _parse_dms_coord(dms: Any, ref: Any) -> float | None:
    if not isinstance(dms, (tuple, list)) or len(dms) != 3:
        return None
    deg = _rational_to_float(dms[0])
    minutes = _rational_to_float(dms[1])
    seconds = _rational_to_float(dms[2])
    if deg is None or minutes is None or seconds is None:
        return None
    if deg < 0 or minutes < 0 or seconds < 0:
        return None
    decimal = deg + minutes / 60.0 + seconds / 3600.0
    if not math.isfinite(decimal):
        return None

    ref_str = ""
    if isinstance(ref, bytes):
        ref_str = ref.decode("ascii", errors="ignore").strip().upper()
    elif isinstance(ref, str):
        ref_str = ref.strip().upper()
    if ref_str in {"S", "W"}:
        decimal = -decimal
    elif ref_str not in {"N", "E", ""}:
        return None
    return round(decimal, 7)


def _parse_exif_offset(raw: Any) -> timezone | None:
    if isinstance(raw, bytes):
        raw = raw.decode("ascii", errors="ignore")
    if not isinstance(raw, str):
        return None
    cleaned = raw.replace("\x00", "").strip()
    if not cleaned:
        return None
    if cleaned.upper() == "Z":
        return UTC
    match = _OFFSET_RE.fullmatch(cleaned)
    if not match:
        return None
    sign = -1 if match.group(1) == "-" else 1
    hours = int(match.group(2))
    minutes = int(match.group(3))
    if hours > 23 or minutes > 59:
        return None
    return timezone(timedelta(hours=sign * hours, minutes=sign * minutes))


def _parse_exif_datetime(raw: Any, offset_raw: Any = None) -> datetime | None:
    if isinstance(raw, bytes):
        raw = raw.decode("ascii", errors="ignore")
    if not isinstance(raw, str):
        return None
    cleaned = raw.replace("\x00", "").strip()
    if len(cleaned) < 19:
        return None
    prefix = cleaned[:19]
    dt: datetime | None = None
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.strptime(prefix, fmt)
            break
        except ValueError:
            continue
    if dt is None:
        return None
    tz = _parse_exif_offset(offset_raw) or UTC
    return dt.replace(tzinfo=tz).astimezone(UTC)


def _parse_gps_timestamp(date_raw: Any, time_raw: Any) -> datetime | None:
    if isinstance(date_raw, bytes):
        date_raw = date_raw.decode("ascii", errors="ignore")
    if not isinstance(date_raw, str):
        return None
    cleaned_date = date_raw.replace("\x00", "").strip()
    if not isinstance(time_raw, (tuple, list)) or len(time_raw) != 3:
        return None
    hh = _rational_to_float(time_raw[0])
    mm = _rational_to_float(time_raw[1])
    ss = _rational_to_float(time_raw[2])
    if hh is None or mm is None or ss is None:
        return None
    try:
        base_date = datetime.strptime(cleaned_date[:10], "%Y:%m:%d")
    except ValueError:
        return None
    total_seconds = hh * 3600.0 + mm * 60.0 + ss
    if not math.isfinite(total_seconds) or total_seconds < 0 or total_seconds >= 86400:
        return None
    return (base_date + timedelta(seconds=total_seconds)).replace(tzinfo=UTC)


def _clean_exif_str(raw: Any) -> str:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="ignore")
    if not isinstance(raw, str):
        return ""
    return " ".join(raw.replace("\x00", "").strip().split())


def _extract_exif_metadata(
    img: Image.Image,
) -> tuple[datetime | None, float | None, float | None, str | None]:
    exif = img.getexif()
    if not exif:
        return None, None, None, None

    make = _clean_exif_str(exif.get(ExifTags.Base.Make))
    model = _clean_exif_str(exif.get(ExifTags.Base.Model))
    camera_make_model: str | None = None
    if make and model:
        if model.lower().startswith(make.lower()):
            camera_make_model = model[:128]
        else:
            camera_make_model = f"{make} {model}"[:128]
    elif model or make:
        camera_make_model = (model or make)[:128]

    exif_ifd = exif.get_ifd(ExifTags.IFD.Exif)
    gps_ifd = exif.get_ifd(ExifTags.IFD.GPSInfo)

    taken_at_utc: datetime | None = None
    dt_candidates = (
        (exif_ifd.get(36867), exif_ifd.get(36881) or exif_ifd.get(36880)),
        (exif_ifd.get(36868), exif_ifd.get(36882) or exif_ifd.get(36880)),
        (exif.get(306), exif_ifd.get(36880)),
    )
    for dt_raw, offset_raw in dt_candidates:
        if dt_raw is not None:
            parsed_dt = _parse_exif_datetime(dt_raw, offset_raw)
            if parsed_dt is not None:
                taken_at_utc = parsed_dt
                break

    if taken_at_utc is None and gps_ifd:
        taken_at_utc = _parse_gps_timestamp(gps_ifd.get(29), gps_ifd.get(7))

    lat: float | None = None
    lon: float | None = None
    if gps_ifd:
        parsed_lat = _parse_dms_coord(gps_ifd.get(2), gps_ifd.get(1))
        parsed_lon = _parse_dms_coord(gps_ifd.get(4), gps_ifd.get(3))
        if (
            parsed_lat is not None
            and parsed_lon is not None
            and -90.0 <= parsed_lat <= 90.0
            and -180.0 <= parsed_lon <= 180.0
        ):
            lat = parsed_lat
            lon = parsed_lon

    return taken_at_utc, lat, lon, camera_make_model


def process_uploaded_image(raw_bytes: bytes) -> ProcessedPhoto:
    """Validate image magic bytes, extract EXIF, strip all metadata, and encode clean WebPs."""
    if not raw_bytes or not _has_allowed_image_magic(raw_bytes):
        raise HTTPException(
            status_code=422,
            detail="Unsupported or invalid image format; only JPEG, PNG, and WEBP are allowed",
        )

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw_bytes), formats=_ALLOWED_PILLOW_FORMATS) as probe:
                probe.verify()

            with Image.open(io.BytesIO(raw_bytes), formats=_ALLOWED_PILLOW_FORMATS) as img:
                if img.format not in {"JPEG", "PNG", "WEBP"}:
                    raise HTTPException(
                        status_code=422,
                        detail="Unsupported image format; only JPEG, PNG, and WEBP are allowed",
                    )
                w, h = img.size
                if w <= 0 or h <= 0 or (w * h) > MAX_PHOTO_PIXELS:
                    raise HTTPException(
                        status_code=422,
                        detail="Image dimensions are invalid or exceed 40 MP limit",
                    )
                img.load()
                taken_at_utc, lat, lon, camera_make_model = _extract_exif_metadata(img)

                transposed = ImageOps.exif_transpose(img)
                if transposed is None:
                    transposed = img
                has_alpha = transposed.mode in {"RGBA", "LA", "PA"} or (
                    transposed.mode == "P" and "transparency" in transposed.info
                )
                target_mode = "RGBA" if has_alpha else "RGB"
                converted = (
                    transposed
                    if transposed.mode == target_mode
                    else transposed.convert(target_mode)
                )
                # Fresh pixel-only Image guarantees zero EXIF/ICC/XMP/polyglot carry-over.
                clean = Image.frombytes(target_mode, converted.size, converted.tobytes())

                full_img = clean.copy()
                full_img.thumbnail((FULL_MAX_DIM, FULL_MAX_DIM), Image.Resampling.LANCZOS)
                full_buf = io.BytesIO()
                full_img.save(full_buf, format="WEBP", quality=85, method=4)
                full_bytes = full_buf.getvalue()
                out_w, out_h = full_img.size

                thumb_img = clean.copy()
                thumb_img.thumbnail((THUMB_MAX_DIM, THUMB_MAX_DIM), Image.Resampling.LANCZOS)
                thumb_buf = io.BytesIO()
                thumb_img.save(thumb_buf, format="WEBP", quality=80, method=4)
                thumb_bytes = thumb_buf.getvalue()
    except HTTPException:
        raise
    except (
        UnidentifiedImageError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        OSError,
        ValueError,
        SyntaxError,
    ) as exc:
        raise HTTPException(
            status_code=422,
            detail="Invalid, corrupted, or unsafe image payload",
        ) from exc

    return ProcessedPhoto(
        full_bytes=full_bytes,
        thumb_bytes=thumb_bytes,
        width=out_w,
        height=out_h,
        taken_at_utc=taken_at_utc,
        lat=lat,
        lon=lon,
        camera_make_model=camera_make_model,
    )


async def _read_bounded_photo_upload(upload: UploadFile, max_bytes: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await upload.read(_CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(
                status_code=413,
                detail=f"Photo exceeds maximum allowed size of {max_bytes} bytes",
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _solve_clock_offset_seconds(
    photos: list[ProjectPhoto],
    stage_parquets: list[tuple[Any, Any]],
    *,
    camera_make_model: str | None = None,
) -> int:
    if not photos or not stage_parquets:
        return 0
    photo_inputs = [
        PhotoAlignmentInput(
            id=str(p.id),
            taken_at_utc=ensure_utc(p.taken_at_utc) if p.taken_at_utc else None,
            lat=p.lat,
            lon=p.lon,
            exif_offset_s=0,
            camera_make_model=getattr(p, "camera_make_model", None),
            match_method="unmatched",
        )
        for p in photos
    ]
    stage_inputs = [
        StageTrackInput(
            stage_id=str(st.id),
            stage_order=st.stage_order,
            parquet_path=pp,
            trim_start_s=st.trim_start_s,
            trim_end_s=st.trim_end_s,
        )
        for st, pp in stage_parquets
    ]
    res = solve_camera_clock_skew(
        photo_inputs,
        stage_inputs,
        camera_make_model=camera_make_model,
    )
    return int(res.offset_seconds)


@router.get("", response_model=list[ProjectPhotoOut])
def list_project_photos(
    project_id: uuid.UUID,
    auth: CurrentAuth,
    db: DbSession,
) -> list[ProjectPhotoOut]:
    project = get_owned_project(db, project_id, auth.user.id)
    photos = get_project_photos(db, project.id)
    return [photo_to_out(p) for p in photos]


@router.post("", response_model=ProjectPhotoOut, status_code=201)
async def upload_project_photo(
    project_id: uuid.UUID,
    request: Request,
    file: Annotated[UploadFile, File(...)],
    auth: CsrfAuth,
    db: DbSession,
    settings: AppSettings,
    store: StoreDep,
    caption: Annotated[str | None, Form(max_length=280)] = None,
    taken_at_utc: Annotated[datetime | None, Form()] = None,
    lat: Annotated[float | None, Form(ge=-90.0, le=90.0, allow_inf_nan=False)] = None,
    lon: Annotated[float | None, Form(ge=-180.0, le=180.0, allow_inf_nan=False)] = None,
) -> ProjectPhotoOut:
    project = get_owned_project(db, project_id, auth.user.id)
    check_rate_limit(
        db,
        bucket_key=f"photo:{auth.user.id}",
        limit=settings.rate_limit_photo_max,
        window_seconds=settings.rate_limit_photo_window_s,
    )

    if (lat is None) != (lon is None):
        raise HTTPException(status_code=422, detail="lat and lon must be provided together")

    existing_count = int(
        db.scalar(
            select(func.count())
            .select_from(ProjectPhoto)
            .where(ProjectPhoto.project_id == project.id)
        )
        or 0
    )
    if existing_count >= MAX_PHOTOS_PER_PROJECT:
        raise HTTPException(
            status_code=422,
            detail=f"Maximum of {MAX_PHOTOS_PER_PROJECT} photos per project reached",
        )

    content_len_raw = request.headers.get("content-length")
    if content_len_raw is not None:
        try:
            content_len = int(content_len_raw)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid Content-Length header") from exc
        if content_len > MAX_PHOTO_BYTES + _MULTIPART_OVERHEAD_BYTES:
            raise HTTPException(status_code=413, detail="Upload payload exceeds 15 MiB limit")

    raw_bytes = await _read_bounded_photo_upload(file, MAX_PHOTO_BYTES)
    if not raw_bytes:
        raise HTTPException(status_code=422, detail="Uploaded photo file is empty")

    content_hash = hashlib.sha256(raw_bytes).hexdigest()
    duplicate = db.scalar(
        select(ProjectPhoto.id).where(
            ProjectPhoto.project_id == project.id,
            ProjectPhoto.content_hash == content_hash,
        )
    )
    if duplicate is not None:
        raise HTTPException(status_code=409, detail="Duplicate photo content")

    processed = await asyncio.to_thread(process_uploaded_image, raw_bytes)
    legacy_keys = list(
        db.scalars(
            select(ProjectPhoto.object_key).where(
                ProjectPhoto.project_id == project.id,
                ProjectPhoto.content_hash.is_(None),
            )
        )
    )
    # Legacy originals were discarded, so compare the deterministic stored image instead.
    for legacy_key in legacy_keys:
        try:
            legacy_bytes = await asyncio.to_thread(store.get_bytes, legacy_key)
        except FileNotFoundError:
            continue
        if legacy_bytes == processed.full_bytes:
            raise HTTPException(status_code=409, detail="Duplicate photo content")
    final_taken_at = (
        ensure_utc(taken_at_utc) if taken_at_utc is not None else processed.taken_at_utc
    )
    has_manual_gps = lat is not None and lon is not None
    final_lat = lat if has_manual_gps else processed.lat
    final_lon = lon if has_manual_gps else processed.lon
    initial_match_method = "manual" if has_manual_gps else "unmatched"

    photo_id = uuid.uuid4()
    object_key = f"photos/{project.id}/{photo_id}.webp"
    thumb_key = f"photos/{project.id}/{photo_id}_thumb.webp"

    await asyncio.to_thread(
        store.put_bytes, object_key, processed.full_bytes, content_type="image/webp"
    )
    try:
        await asyncio.to_thread(
            store.put_bytes, thumb_key, processed.thumb_bytes, content_type="image/webp"
        )

        # Concurrent uploads must serialize the final deduplication and quota checks.
        is_postgres = db.get_bind().dialect.name == "postgresql"
        lock_stmt = select(Project).where(
            Project.id == project.id,
            Project.owner_user_id == auth.user.id,
        )
        if is_postgres:
            lock_stmt = lock_stmt.with_for_update()
        locked_project = db.scalar(lock_stmt)
        if locked_project is None:
            raise HTTPException(status_code=404, detail="Project not found")

        duplicate = db.scalar(
            select(ProjectPhoto.id).where(
                ProjectPhoto.project_id == project.id,
                ProjectPhoto.content_hash == content_hash,
            )
        )
        if duplicate is not None:
            raise HTTPException(status_code=409, detail="Duplicate photo content")

        authoritative_count = int(
            db.scalar(
                select(func.count())
                .select_from(ProjectPhoto)
                .where(ProjectPhoto.project_id == project.id)
            )
            or 0
        )
        if authoritative_count >= MAX_PHOTOS_PER_PROJECT:
            raise HTTPException(
                status_code=422,
                detail=f"Maximum of {MAX_PHOTOS_PER_PROJECT} photos per project reached",
            )

        photo = ProjectPhoto(
            id=photo_id,
            project_id=project.id,
            source_type="upload",
            object_key=object_key,
            thumb_object_key=thumb_key,
            mime_type="image/webp",
            width=processed.width,
            height=processed.height,
            byte_size=len(processed.full_bytes),
            content_hash=content_hash,
            caption=(caption or "").strip()[:280],
            taken_at_utc=final_taken_at,
            lat=final_lat,
            lon=final_lon,
            match_method=initial_match_method,
            camera_make_model=processed.camera_make_model,
            exif_offset_s=0,
            enabled=True,
        )
        db.add(photo)
        write_audit_event(
            db,
            user_id=auth.user.id,
            project_id=project.id,
            event_type="photo.uploaded",
            metadata={
                "photo_id": str(photo_id),
                "width": processed.width,
                "height": processed.height,
                "has_gps": final_lat is not None and final_lon is not None,
                "has_timestamp": final_taken_at is not None,
                "camera_make_model": processed.camera_make_model,
            },
        )
        db.commit()
        db.refresh(photo)
    except Exception:
        db.rollback()
        for k in (object_key, thumb_key):
            try:
                await asyncio.to_thread(store.delete, k)
            except Exception:
                logger.warning("Failed to rollback photo key %s", k)
        raise

    if not has_manual_gps:
        try:
            align_project_photos(db, store, project.id, photo_ids={photo.id}, persist=True)
            db.refresh(photo)
        except Exception as exc:
            logger.warning("Photo auto-alignment failed on upload %s: %s", photo.id, exc)

    return photo_to_out(photo)


@router.post("/shift-clock", response_model=PhotoShiftClockOut)
def shift_photo_clocks(
    project_id: uuid.UUID,
    body: PhotoShiftClockRequest,
    auth: CsrfAuth,
    db: DbSession,
    store: StoreDep,
) -> PhotoShiftClockOut:
    project = get_owned_project(db, project_id, auth.user.id)
    all_photos = get_project_photos(db, project.id)
    stages = get_project_stages(db, project.id)

    photo_id_filter = set(body.photo_ids) if body.photo_ids is not None else None
    target_photos = [
        p
        for p in all_photos
        if p.match_method != "manual"
        and (photo_id_filter is None or p.id in photo_id_filter)
        and (
            body.camera_make_model is None
            or getattr(p, "camera_make_model", None) == body.camera_make_model
        )
    ]

    matched_before = sum(1 for p in all_photos if p.match_method != "unmatched")

    if body.offset_seconds is not None:
        applied_offset = body.offset_seconds
        offsets_by_id = {p.id: applied_offset for p in target_photos}
    else:
        with materialize_silver_parquets(store, stages) as pairs:
            if body.camera_make_model is None:
                grouped: dict[str | None, list[ProjectPhoto]] = {}
                for p in target_photos:
                    grouped.setdefault(getattr(p, "camera_make_model", None), []).append(p)
                offsets_by_id = {}
                solved_values: list[int] = []
                for _cam, cam_photos in grouped.items():
                    cam_off = _solve_clock_offset_seconds(cam_photos, pairs)
                    solved_values.append(cam_off)
                    for cp in cam_photos:
                        offsets_by_id[cp.id] = cam_off
                applied_offset = solved_values[0] if solved_values else 0
            else:
                applied_offset = _solve_clock_offset_seconds(target_photos, pairs)
                offsets_by_id = {p.id: applied_offset for p in target_photos}

    target_ids = {p.id for p in target_photos}
    if not body.dry_run:
        for p in target_photos:
            p.exif_offset_s = offsets_by_id.get(p.id, applied_offset)

    align_project_photos(
        db,
        store,
        project.id,
        photo_ids=target_ids,
        override_offsets=offsets_by_id,
        persist=False,
    )

    matched_after = sum(1 for p in all_photos if p.match_method != "unmatched")
    newly_matched = max(0, matched_after - matched_before)
    serialized = [photo_to_out(p) for p in all_photos]

    if body.dry_run:
        db.rollback()
    else:
        write_audit_event(
            db,
            user_id=auth.user.id,
            project_id=project.id,
            event_type="photo.clock_shifted",
            metadata={
                "applied_offset_seconds": applied_offset,
                "target_count": len(target_photos),
                "matched_before": matched_before,
                "matched_after": matched_after,
            },
        )
        db.commit()

    return PhotoShiftClockOut(
        applied_offset_seconds=applied_offset,
        dry_run=body.dry_run,
        matched_before=matched_before,
        matched_after=matched_after,
        newly_matched_count=newly_matched,
        photos=serialized,
    )


@router.post("/{photo_id}/pin", response_model=ProjectPhotoOut)
def pin_project_photo(
    project_id: uuid.UUID,
    photo_id: uuid.UUID,
    body: PhotoPinRequest,
    auth: CsrfAuth,
    db: DbSession,
    store: StoreDep,
) -> ProjectPhotoOut:
    project = get_owned_project(db, project_id, auth.user.id)
    photo = db.scalar(
        select(ProjectPhoto).where(
            ProjectPhoto.id == photo_id,
            ProjectPhoto.project_id == project.id,
        )
    )
    if photo is None:
        raise HTTPException(status_code=404, detail="Photo not found")

    stages = get_project_stages(db, project.id)
    if not stages:
        raise HTTPException(
            status_code=422,
            detail="Cannot pin photo on a project with zero stages",
        )

    with materialize_silver_parquets(store, stages) as pairs:
        stage_inputs = [
            StageTrackInput(
                stage_id=str(st.id),
                stage_order=st.stage_order,
                parquet_path=pp,
                trim_start_s=st.trim_start_s,
                trim_end_s=st.trim_end_s,
            )
            for st, pp in pairs
        ]
        try:
            snapped = snap_photo_pin(
                stage_inputs,
                lat=body.lat,
                lon=body.lon,
                progress=body.progress,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    snapped_stage_id = uuid.UUID(snapped.stage_id)
    snapped_lat = snapped.matched_lat
    snapped_lon = snapped.matched_lon
    snapped_progress = snapped.matched_progress

    photo.stage_id = snapped_stage_id
    if body.lat is not None and body.lon is not None:
        photo.lat = body.lat
        photo.lon = body.lon
    elif photo.lat is None or photo.lon is None:
        photo.lat = snapped_lat
        photo.lon = snapped_lon
    photo.matched_lat = snapped_lat
    photo.matched_lon = snapped_lon
    photo.matched_progress = snapped_progress
    photo.match_method = "manual"

    write_audit_event(
        db,
        user_id=auth.user.id,
        project_id=project.id,
        event_type="photo.pinned",
        metadata={
            "photo_id": str(photo.id),
            "stage_id": str(snapped_stage_id) if snapped_stage_id else None,
            "matched_progress": snapped_progress,
        },
    )
    db.commit()
    db.refresh(photo)
    return photo_to_out(photo)


@router.patch("/{photo_id}", response_model=ProjectPhotoOut)
def update_project_photo(
    project_id: uuid.UUID,
    photo_id: uuid.UUID,
    body: ProjectPhotoUpdateRequest,
    auth: CsrfAuth,
    db: DbSession,
) -> ProjectPhotoOut:
    project = get_owned_project(db, project_id, auth.user.id)
    photo = db.scalar(
        select(ProjectPhoto).where(
            ProjectPhoto.id == photo_id,
            ProjectPhoto.project_id == project.id,
        )
    )
    if photo is None:
        raise HTTPException(status_code=404, detail="Photo not found")

    if "caption" in body.model_fields_set:
        photo.caption = (body.caption or "").strip()[:280]
    if "taken_at_utc" in body.model_fields_set:
        photo.taken_at_utc = ensure_utc(body.taken_at_utc) if body.taken_at_utc else None
    if "lat" in body.model_fields_set and "lon" in body.model_fields_set:
        photo.lat = body.lat
        photo.lon = body.lon
        photo.matched_lat = None
        photo.matched_lon = None
        photo.matched_progress = None
        photo.match_method = "manual" if body.lat is not None else "unmatched"
    if "enabled" in body.model_fields_set and body.enabled is not None:
        photo.enabled = body.enabled

    write_audit_event(
        db,
        user_id=auth.user.id,
        project_id=project.id,
        event_type="photo.updated",
        metadata={"photo_id": str(photo.id), "enabled": photo.enabled},
    )
    db.commit()
    db.refresh(photo)
    return photo_to_out(photo)


@router.delete("/{photo_id}", status_code=204)
def delete_project_photo(
    project_id: uuid.UUID,
    photo_id: uuid.UUID,
    auth: CsrfAuth,
    db: DbSession,
    store: StoreDep,
) -> Response:
    project = get_owned_project(db, project_id, auth.user.id)
    photo = db.scalar(
        select(ProjectPhoto).where(
            ProjectPhoto.id == photo_id,
            ProjectPhoto.project_id == project.id,
        )
    )
    if photo is None:
        raise HTTPException(status_code=404, detail="Photo not found")

    keys_to_delete = [photo.object_key, photo.thumb_object_key]
    write_audit_event(
        db,
        user_id=auth.user.id,
        project_id=project.id,
        event_type="photo.deleted",
        metadata={"photo_id": str(photo.id)},
    )
    db.delete(photo)
    db.commit()

    for key in keys_to_delete:
        try:
            store.delete(key)
        except Exception:
            logger.warning("Failed to delete photo object key %s", key)

    return Response(status_code=204)


@router.get("/{photo_id}/image")
def get_project_photo_image(
    project_id: uuid.UUID,
    photo_id: uuid.UUID,
    auth: CurrentAuth,
    db: DbSession,
    store: StoreDep,
    variant: Annotated[Literal["full", "thumb"], Query()] = "full",
) -> Response:
    project = get_owned_project(db, project_id, auth.user.id)
    photo = db.scalar(
        select(ProjectPhoto).where(
            ProjectPhoto.id == photo_id,
            ProjectPhoto.project_id == project.id,
        )
    )
    if photo is None:
        raise HTTPException(status_code=404, detail="Photo not found")

    key = photo.thumb_object_key if variant == "thumb" else photo.object_key
    try:
        data = store.get_bytes(key)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Photo artifact missing") from exc

    return Response(
        content=data,
        media_type="image/webp",
        headers={
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, max-age=3600",
        },
    )
