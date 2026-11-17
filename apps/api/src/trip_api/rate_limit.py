"""PostgreSQL-backed sliding/fixed-window rate limiter."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from trip_api.db import RateLimitRecord


def _ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def check_rate_limit(
    db: Session,
    *,
    bucket_key: str,
    limit: int,
    window_seconds: int,
    cost: int = 1,
) -> None:
    """Enforce a persistent database-backed rate limit for a bucket key."""
    if cost < 1:
        raise ValueError("cost must be >= 1")
    if limit <= 0 or window_seconds <= 0:
        return

    now = datetime.now(UTC)
    is_postgres = db.get_bind().dialect.name == "postgresql"

    stmt = select(RateLimitRecord).where(RateLimitRecord.bucket_key == bucket_key)
    if is_postgres:
        stmt = stmt.with_for_update()

    record = db.scalar(stmt)
    if record is None:
        if cost > limit:
            raise HTTPException(
                status_code=429,
                detail="Rate limit exceeded. Please try again later.",
                headers={"Retry-After": str(window_seconds)},
            )
        record = RateLimitRecord(
            bucket_key=bucket_key,
            window_start=now,
            request_count=cost,
            updated_at=now,
        )
        db.add(record)
        try:
            db.commit()
            return
        except IntegrityError:
            db.rollback()
            record = db.scalar(stmt)
            if record is None:
                raise

    window_start = _ensure_utc(record.window_start)
    elapsed = (now - window_start).total_seconds()
    if elapsed >= window_seconds:
        if cost > limit:
            raise HTTPException(
                status_code=429,
                detail="Rate limit exceeded. Please try again later.",
                headers={"Retry-After": str(window_seconds)},
            )
        record.window_start = now
        record.request_count = cost
        record.updated_at = now
        db.commit()
        return

    if record.request_count + cost > limit:
        retry_after = max(1, int(window_seconds - elapsed))
        raise HTTPException(
            status_code=429,
            detail="Rate limit exceeded. Please try again later.",
            headers={"Retry-After": str(retry_after)},
        )

    record.request_count += cost
    record.updated_at = now
    db.commit()
