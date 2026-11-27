from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal, cast

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from trip_api.db import (
    AuditEvent,
    CreditLedgerEntry,
    CreditVoucher,
    CreditVoucherRedemption,
    RenderJob,
    User,
)
from trip_api.deps import ensure_utc
from trip_api.schemas import CreditLedgerEntryOut, CreditLedgerEntryType

_VOUCHER_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_MAX_INT32 = 2_147_483_647
_MIN_INT32 = -2_147_483_648


@dataclass(frozen=True, slots=True)
class CreatedVoucherInfo:
    id: uuid.UUID
    code: str
    code_prefix: str
    credits_amount: int
    max_redemptions: int
    expires_at: datetime | None
    note: str


def normalize_voucher_code(raw_code: str) -> str:
    return raw_code.strip().upper()


def hash_voucher_code(normalized_code: str) -> str:
    return hashlib.sha256(normalized_code.encode("utf-8")).hexdigest()


def generate_voucher_code() -> str:
    groups = ["".join(secrets.choice(_VOUCHER_ALPHABET) for _ in range(4)) for _ in range(3)]
    return f"TRIP-{'-'.join(groups)}"


def ledger_entry_to_out(entry: CreditLedgerEntry) -> CreditLedgerEntryOut:
    return CreditLedgerEntryOut(
        id=entry.id,
        delta=entry.delta,
        entry_type=cast(CreditLedgerEntryType, entry.entry_type),
        render_job_id=entry.render_job_id,
        voucher_id=entry.voucher_id,
        note=entry.note,
        created_at=ensure_utc(entry.created_at),
    )


def get_user_credit_balance(db: Session, user_id: uuid.UUID) -> int:
    total = db.scalar(
        select(func.coalesce(func.sum(CreditLedgerEntry.delta), 0)).where(
            CreditLedgerEntry.user_id == user_id
        )
    )
    return int(total or 0)


def list_user_credit_ledger(
    db: Session,
    user_id: uuid.UUID,
    *,
    limit: int = 50,
) -> list[CreditLedgerEntry]:
    return list(
        db.scalars(
            select(CreditLedgerEntry)
            .where(CreditLedgerEntry.user_id == user_id)
            .order_by(CreditLedgerEntry.created_at.desc(), CreditLedgerEntry.id.desc())
            .limit(limit)
        ).all()
    )


def lock_user_for_credit_update(db: Session, user_id: uuid.UUID) -> None:
    if db.get_bind().dialect.name == "postgresql":
        db.scalar(select(User.id).where(User.id == user_id).with_for_update())


def debit_render_credits(
    db: Session,
    *,
    user_id: uuid.UUID,
    job_id: uuid.UUID,
    cost: int,
    resolution: str,
    fps: int,
    already_locked: bool = False,
) -> CreditLedgerEntry:
    if cost <= 0:
        raise ValueError("Render credit debit cost must be positive")
    if not already_locked:
        lock_user_for_credit_update(db, user_id)
        balance = get_user_credit_balance(db, user_id)
        if balance < cost:
            raise HTTPException(
                status_code=402,
                detail=(
                    f"Insufficient Render Credits: this export requires {cost} credits, "
                    f"but your balance is {balance}. Redeem a voucher or use free Local Export."
                ),
            )
    entry = CreditLedgerEntry(
        user_id=user_id,
        delta=-cost,
        entry_type="render_debit",
        render_job_id=job_id,
        idempotency_key=f"render_debit:{job_id}",
        note=f"Server Render ({resolution} @ {fps} fps)",
    )
    db.add(entry)
    return entry


def refund_render_credits_idempotent(
    db: Session,
    *,
    job: RenderJob,
    note: str,
) -> bool:
    charged = int(getattr(job, "credits_charged", 0) or 0)
    if charged <= 0:
        return False
    idem_key = f"render_refund:{job.id}"
    existing = db.scalar(
        select(CreditLedgerEntry.id).where(CreditLedgerEntry.idempotency_key == idem_key)
    )
    if existing is not None:
        return False

    try:
        with db.begin_nested():
            db.add(
                CreditLedgerEntry(
                    user_id=job.owner_user_id,
                    delta=charged,
                    entry_type="render_refund",
                    render_job_id=job.id,
                    idempotency_key=idem_key,
                    note=note[:280],
                )
            )
            db.flush()
    except IntegrityError:
        return False
    return True


def redeem_voucher(
    db: Session,
    *,
    user_id: uuid.UUID,
    raw_code: str,
) -> tuple[CreditVoucher, CreditLedgerEntry, int]:
    normalized = normalize_voucher_code(raw_code)
    if len(normalized) < 4:
        raise HTTPException(status_code=400, detail="Invalid voucher code")
    code_hash = hash_voucher_code(normalized)

    stmt = select(CreditVoucher).where(CreditVoucher.code_hash == code_hash)
    if db.get_bind().dialect.name == "postgresql":
        stmt = stmt.with_for_update()
    voucher = db.scalar(stmt)
    now = datetime.now(UTC)
    if voucher is None or voucher.revoked_at is not None:
        raise HTTPException(status_code=400, detail="Invalid or revoked voucher code")
    if voucher.expires_at is not None and ensure_utc(voucher.expires_at) <= now:
        raise HTTPException(status_code=400, detail="This voucher code has expired")

    already_redeemed = db.scalar(
        select(CreditVoucherRedemption.id).where(
            CreditVoucherRedemption.voucher_id == voucher.id,
            CreditVoucherRedemption.user_id == user_id,
        )
    )
    if already_redeemed is not None:
        raise HTTPException(status_code=409, detail="You have already redeemed this voucher code")

    if voucher.redemption_count >= voucher.max_redemptions:
        raise HTTPException(status_code=400, detail="This voucher code has no redemptions left")

    lock_user_for_credit_update(db, user_id)
    voucher.redemption_count += 1
    db.add(
        CreditVoucherRedemption(
            voucher_id=voucher.id,
            user_id=user_id,
            redeemed_at=now,
        )
    )
    entry = CreditLedgerEntry(
        user_id=user_id,
        delta=voucher.credits_amount,
        entry_type="voucher_grant",
        voucher_id=voucher.id,
        idempotency_key=f"voucher_grant:{voucher.id}:{user_id}",
        note=f"Redeemed voucher {voucher.code_prefix}...",
    )
    db.add(entry)
    db.add(
        AuditEvent(
            user_id=user_id,
            event_type="credits.voucher_redeemed",
            metadata_json={
                "voucher_id": str(voucher.id),
                "code_prefix": voucher.code_prefix,
                "credits_amount": voucher.credits_amount,
            },
        )
    )
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="You have already redeemed this voucher code",
        ) from exc

    db.refresh(entry)
    new_balance = get_user_credit_balance(db, user_id)
    return voucher, entry, new_balance


def create_voucher_record(
    db: Session,
    *,
    credits_amount: int,
    max_redemptions: int = 1,
    code: str | None = None,
    expires_in_days: int | None = None,
    note: str = "",
) -> CreatedVoucherInfo:
    if credits_amount <= 0 or credits_amount > _MAX_INT32:
        raise ValueError("credits_amount must be between 1 and 2147483647")
    if max_redemptions < 1 or max_redemptions > _MAX_INT32:
        raise ValueError("max_redemptions must be between 1 and 2147483647")
    if expires_in_days is not None and (expires_in_days < 1 or expires_in_days > 36500):
        raise ValueError("expires_in_days must be between 1 and 36500")

    normalized_code = normalize_voucher_code(code) if code is not None else generate_voucher_code()
    if len(normalized_code) < 6 or len(normalized_code) > 128:
        raise ValueError("Voucher code must be 6-128 characters")
    code_hash = hash_voucher_code(normalized_code)
    prefix_len = min(5, max(2, len(normalized_code) // 3))
    code_prefix = normalized_code[:prefix_len]
    expires_at = (
        datetime.now(UTC) + timedelta(days=expires_in_days) if expires_in_days is not None else None
    )

    voucher = CreditVoucher(
        code_hash=code_hash,
        code_prefix=code_prefix,
        credits_amount=credits_amount,
        max_redemptions=max_redemptions,
        redemption_count=0,
        expires_at=expires_at,
        note=note.strip()[:280],
    )
    db.add(voucher)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise ValueError("A voucher with that code already exists") from exc
    db.refresh(voucher)
    return CreatedVoucherInfo(
        id=voucher.id,
        code=normalized_code,
        code_prefix=voucher.code_prefix,
        credits_amount=voucher.credits_amount,
        max_redemptions=voucher.max_redemptions,
        expires_at=ensure_utc(voucher.expires_at) if voucher.expires_at else None,
        note=voucher.note,
    )


def revoke_voucher_record(db: Session, *, voucher_id: uuid.UUID) -> CreditVoucher:
    voucher = db.get(CreditVoucher, voucher_id)
    if voucher is None:
        raise ValueError(f"Voucher {voucher_id} not found")
    if voucher.revoked_at is None:
        voucher.revoked_at = datetime.now(UTC)
        db.commit()
        db.refresh(voucher)
    return voucher


def grant_user_credits(
    db: Session,
    *,
    email: str,
    delta: int,
    note: str = "",
) -> tuple[User, CreditLedgerEntry, int]:
    if delta == 0 or delta < _MIN_INT32 or delta > _MAX_INT32:
        raise ValueError("delta must be a non-zero 32-bit integer")
    normalized_email = email.strip().lower()
    user = db.scalar(select(User).where(User.email == normalized_email))
    if user is None:
        raise ValueError(f"User with email {normalized_email!r} not found")

    lock_user_for_credit_update(db, user.id)
    entry_type: Literal["admin_adjustment"] = "admin_adjustment"
    entry = CreditLedgerEntry(
        user_id=user.id,
        delta=delta,
        entry_type=entry_type,
        note=(note.strip() or "Operator credit adjustment")[:280],
    )
    db.add(entry)
    db.add(
        AuditEvent(
            user_id=user.id,
            event_type="credits.admin_adjustment",
            metadata_json={"delta": delta, "note": entry.note},
        )
    )
    db.commit()
    db.refresh(entry)
    balance = get_user_credit_balance(db, user.id)
    return user, entry, balance
