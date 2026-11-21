"""Owner-scoped reversible hiding and confirmed catalog disconnection without artifact purge."""

import hashlib
import hmac
import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from trip_api.db import Project, Stage, VaultActivity
from trip_api.deps import AppSettings, CsrfAuth, CurrentAuth, DbSession
from trip_api.vault_sync import bump_user_vault_version

router = APIRouter(prefix="/api/v1/vault/activities", tags=["vault"])


class VisibilityChange(BaseModel):
    """Set reversible visibility without changing any project material."""

    model_config = ConfigDict(extra="forbid")
    hidden: bool = Field(strict=True)


class DisconnectConfirmation(BaseModel):
    """Confirm the exact previously reviewed source and project-usage snapshot."""

    model_config = ConfigDict(extra="forbid")
    confirmation: str = Field(min_length=64, max_length=64)


def _owned(db: DbSession, owner_id: UUID, activity_id: UUID) -> VaultActivity:
    activity = db.scalar(
        select(VaultActivity)
        .where(
            VaultActivity.user_id == owner_id,
            VaultActivity.id == activity_id,
            VaultActivity.disconnected_at.is_(None),
        )
        .with_for_update()
    )
    if activity is None:
        raise HTTPException(status_code=404, detail="Activity not found")
    return activity


def _impact(db: DbSession, activity: VaultActivity, secret: str) -> dict[str, Any]:
    usages = db.execute(
        select(Stage, Project)
        .join(Project, Stage.project_id == Project.id)
        .where(
            Project.owner_user_id == activity.user_id,
            Stage.vault_activity_id == activity.id,
        )
        .order_by(Stage.id)
    ).all()
    projects = [
        {"project_id": str(project.id), "title": project.title, "stage_id": str(stage.id)}
        for stage, project in usages
    ]
    snapshot = json.dumps(
        [
            str(activity.user_id),
            str(activity.id),
            activity.updated_at.isoformat(),
            activity.hidden_at.isoformat() if activity.hidden_at else None,
            [(str(stage.id), stage.name, project.title) for stage, project in usages],
        ],
        separators=(",", ":"),
    )
    confirmation = hmac.new(secret.encode(), snapshot.encode(), hashlib.sha256).hexdigest()
    return {
        "operation": "disconnect",
        "confirmation": confirmation,
        "project_usages": projects,
        "preserves_project_material": True,
        "permanent_purge_available": False,
        "retention_notice": (
            "Removes the activity from the catalog and suppresses reimport. "
            "Stored source data and project copies are retained. "
            "This is not permanent data deletion."
        ),
    }


@router.patch("/{activity_id}/visibility")
def change_visibility(
    activity_id: UUID, body: VisibilityChange, auth: CsrfAuth, db: DbSession
) -> dict[str, Any]:
    """Hide or restore an owned activity while preserving its canonical source and usages."""
    activity = _owned(db, auth.user.id, activity_id)
    activity.hidden_at = datetime.now(UTC) if body.hidden else None
    bump_user_vault_version(db, auth.user.id)
    db.commit()
    return {"id": str(activity.id), "hidden": body.hidden}


@router.get("/{activity_id}/disconnect-preview")
def preview_disconnect(
    activity_id: UUID, auth: CurrentAuth, db: DbSession, settings: AppSettings
) -> dict[str, Any]:
    """Explain retention and sign the current owner-scoped project impact."""
    return _impact(db, _owned(db, auth.user.id, activity_id), settings.session_secret)


@router.post("/{activity_id}/disconnect")
def disconnect_activity(
    activity_id: UUID,
    body: DisconnectConfirmation,
    auth: CsrfAuth,
    db: DbSession,
    settings: AppSettings,
) -> dict[str, Any]:
    """Suppress a catalog activity after confirmation without deleting source or project data."""
    activity = _owned(db, auth.user.id, activity_id)
    impact = _impact(db, activity, settings.session_secret)
    if not hmac.compare_digest(body.confirmation, impact["confirmation"]):
        raise HTTPException(
            status_code=409, detail="Activity or project usages changed; review the impact again"
        )
    activity.disconnected_at = datetime.now(UTC)
    bump_user_vault_version(db, auth.user.id)
    db.commit()
    return {"id": str(activity.id), "disconnected": True, "permanently_deleted": False}
