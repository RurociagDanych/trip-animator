"""Private named Activity Library views storing validated, versioned query rules."""

from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Any, Literal, Self
from uuid import UUID

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import select

from trip_api.deps import CsrfAuth, CurrentAuth, DbSession
from trip_api.library_geometry import parse_area
from trip_api.library_view import LibraryView

router = APIRouter(prefix="/api/v1/vault/views", tags=["vault"])


class ViewFilters(BaseModel):
    """Bounded query filters, including dates resolved only when a view opens."""

    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, max_length=200)
    sport: (
        Annotated[str, Field(max_length=32)]
        | Annotated[list[Annotated[str, Field(max_length=32)]], Field(max_length=32)]
        | None
    ) = None
    start_date: date | None = None
    end_date: date | None = None
    relative_days: int | None = Field(default=None, ge=1, le=36500, strict=True)
    min_distance_m: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    max_distance_m: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    min_elevation_gain_m: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    max_elevation_gain_m: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    min_moving_seconds: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    max_moving_seconds: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    has_gps: bool | None = None
    include_hidden: bool = False
    area: str | None = Field(default=None, max_length=160)

    @model_validator(mode="after")
    def validate_ranges(self) -> Self:
        """Reject ambiguous dates and ranges the catalog itself cannot execute."""
        if self.relative_days is not None and (self.start_date or self.end_date):
            raise ValueError("Relative and explicit dates cannot be combined")
        if self.start_date and self.end_date and self.start_date > self.end_date:
            raise ValueError("start_date must be on or before end_date")
        for lower, upper in (
            (self.min_distance_m, self.max_distance_m),
            (self.min_elevation_gain_m, self.max_elevation_gain_m),
            (self.min_moving_seconds, self.max_moving_seconds),
        ):
            if lower is not None and upper is not None and lower > upper:
                raise ValueError("Minimum exceeds maximum")
        if self.area is not None:
            parse_area(self.area)
        return self


class ViewRules(BaseModel):
    """Versioned filter and sort definition independent of current query results."""

    model_config = ConfigDict(extra="forbid")
    version: Literal[1]
    filters: ViewFilters = Field(default_factory=ViewFilters)
    sort: Literal[
        "started_at", "local_date", "name", "distance_m", "elevation_gain_m", "moving_seconds"
    ] = "started_at"
    direction: Literal["asc", "desc"] = "desc"


class ViewCreate(BaseModel):
    """A private human-readable name and validated query definition."""

    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120)
    rules: ViewRules

    @field_validator("name")
    @classmethod
    def nonblank_name(cls, value: str) -> str:
        """Reject names containing only whitespace."""
        value = value.strip()
        if not value:
            raise ValueError("Name must not be blank")
        return value


def _output(view: LibraryView) -> dict[str, Any]:
    rules = ViewRules.model_validate(view.rules)
    resolved = rules.filters.model_dump(mode="json", exclude_unset=True, exclude_none=True)
    days = resolved.pop("relative_days", None)
    if days is not None:
        today = datetime.now(UTC).date()
        resolved.update(
            start_date=(today - timedelta(days=days - 1)).isoformat(), end_date=today.isoformat()
        )
    return {
        "id": str(view.id),
        "name": view.name,
        "rules": view.rules,
        "resolved_filters": resolved,
    }


def _owned(db: DbSession, user_id: UUID, view_id: UUID) -> LibraryView:
    view = db.scalar(
        select(LibraryView).where(LibraryView.id == view_id, LibraryView.user_id == user_id)
    )
    if view is None:
        raise HTTPException(status_code=404, detail="Saved view not found")
    return view


@router.get("")
def list_views(auth: CurrentAuth, db: DbSession) -> dict[str, Any]:
    """List the authenticated owner's private saved definitions."""
    views = db.scalars(
        select(LibraryView)
        .where(LibraryView.user_id == auth.user.id)
        .order_by(LibraryView.created_at, LibraryView.id)
    ).all()
    return {"items": [_output(view) for view in views]}


@router.post("", status_code=201)
def create_view(body: ViewCreate, auth: CsrfAuth, db: DbSession) -> dict[str, Any]:
    """Persist rules without evaluating or freezing the activity selection."""
    view = LibraryView(
        user_id=auth.user.id,
        name=body.name,
        rules=body.rules.model_dump(mode="json", exclude_unset=True, exclude_none=True),
    )
    db.add(view)
    db.commit()
    db.refresh(view)
    return _output(view)


@router.get("/{view_id}")
def open_view(view_id: UUID, auth: CurrentAuth, db: DbSession) -> dict[str, Any]:
    """Resolve relative dates against the current UTC calendar date on opening."""
    return _output(_owned(db, auth.user.id, view_id))


@router.delete("/{view_id}", status_code=204)
def delete_view(view_id: UUID, auth: CsrfAuth, db: DbSession) -> Response:
    """Delete only the owner's definition without touching activities."""
    db.delete(_owned(db, auth.user.id, view_id))
    db.commit()
    return Response(status_code=204)
