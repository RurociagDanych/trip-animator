from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, Request

from trip_api.credits import (
    get_user_credit_balance,
    ledger_entry_to_out,
    list_user_credit_ledger,
    redeem_voucher,
)
from trip_api.deps import AppSettings, CsrfAuth, CurrentAuth, DbSession, get_client_ip
from trip_api.rate_limit import check_rate_limit
from trip_api.schemas import (
    CreditsSummaryOut,
    RedeemVoucherOut,
    RedeemVoucherRequest,
)

router = APIRouter(prefix="/api/v1/credits", tags=["credits"])


@router.get("", response_model=CreditsSummaryOut)
def get_credits_summary(
    auth: CurrentAuth,
    db: DbSession,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> CreditsSummaryOut:
    balance = get_user_credit_balance(db, auth.user.id)
    entries = list_user_credit_ledger(db, auth.user.id, limit=limit)
    return CreditsSummaryOut(
        balance=balance,
        ledger=[ledger_entry_to_out(e) for e in entries],
    )


@router.post("/redeem", response_model=RedeemVoucherOut)
def redeem_credit_voucher(
    body: RedeemVoucherRequest,
    request: Request,
    auth: CsrfAuth,
    db: DbSession,
    settings: AppSettings,
) -> RedeemVoucherOut:
    client_ip = get_client_ip(request)
    check_rate_limit(
        db,
        bucket_key=f"voucher_redeem:user:{auth.user.id}",
        limit=settings.rate_limit_voucher_redeem_max,
        window_seconds=settings.rate_limit_voucher_redeem_window_s,
    )
    check_rate_limit(
        db,
        bucket_key=f"voucher_redeem:ip:{client_ip}",
        limit=settings.rate_limit_voucher_redeem_max,
        window_seconds=settings.rate_limit_voucher_redeem_window_s,
    )

    voucher, entry, new_balance = redeem_voucher(
        db,
        user_id=auth.user.id,
        raw_code=body.code,
    )
    return RedeemVoucherOut(
        credits_granted=voucher.credits_amount,
        balance=new_balance,
        entry=ledger_entry_to_out(entry),
    )
