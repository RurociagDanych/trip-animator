from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015_render_credits_vouchers"
down_revision: str | Sequence[str] | None = "0014_user_identities"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "render_jobs",
        sa.Column(
            "credits_charged",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )

    op.create_table(
        "credit_vouchers",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("code_hash", sa.String(64), nullable=False),
        sa.Column("code_prefix", sa.String(16), nullable=False),
        sa.Column("credits_amount", sa.Integer(), nullable=False),
        sa.Column(
            "max_redemptions",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
        ),
        sa.Column(
            "redemption_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("note", sa.String(280), nullable=False, server_default=""),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("credits_amount > 0", name="ck_credit_vouchers_credits_positive"),
        sa.CheckConstraint("max_redemptions >= 1", name="ck_credit_vouchers_max_redemptions"),
        sa.CheckConstraint(
            "redemption_count >= 0 AND redemption_count <= max_redemptions",
            name="ck_credit_vouchers_redemption_bounds",
        ),
    )
    op.create_index("ix_credit_vouchers_code_hash", "credit_vouchers", ["code_hash"], unique=True)

    op.create_table(
        "credit_voucher_redemptions",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "voucher_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("credit_vouchers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "redeemed_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "voucher_id",
            "user_id",
            name="uq_credit_voucher_redemptions_voucher_user",
        ),
    )
    op.create_index(
        "ix_credit_voucher_redemptions_voucher_id",
        "credit_voucher_redemptions",
        ["voucher_id"],
    )
    op.create_index(
        "ix_credit_voucher_redemptions_user_id",
        "credit_voucher_redemptions",
        ["user_id"],
    )

    op.create_table(
        "credit_ledger_entries",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "user_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("delta", sa.Integer(), nullable=False),
        sa.Column("entry_type", sa.String(32), nullable=False),
        sa.Column(
            "render_job_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("render_jobs.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "voucher_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("credit_vouchers.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("idempotency_key", sa.String(128), nullable=True),
        sa.Column("note", sa.String(280), nullable=False, server_default=""),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("delta != 0", name="ck_credit_ledger_entries_delta_nonzero"),
        sa.CheckConstraint(
            "entry_type IN ('voucher_grant', 'render_debit', 'render_refund', 'admin_adjustment')",
            name="ck_credit_ledger_entries_entry_type",
        ),
        sa.UniqueConstraint("idempotency_key", name="uq_credit_ledger_entries_idempotency_key"),
    )
    op.create_index("ix_credit_ledger_entries_user_id", "credit_ledger_entries", ["user_id"])
    op.create_index(
        "ix_credit_ledger_entries_render_job_id",
        "credit_ledger_entries",
        ["render_job_id"],
    )
    op.create_index(
        "ix_credit_ledger_entries_voucher_id",
        "credit_ledger_entries",
        ["voucher_id"],
    )
    op.create_index(
        "ix_credit_ledger_entries_user_created",
        "credit_ledger_entries",
        ["user_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_credit_ledger_entries_user_created", table_name="credit_ledger_entries")
    op.drop_index("ix_credit_ledger_entries_voucher_id", table_name="credit_ledger_entries")
    op.drop_index("ix_credit_ledger_entries_render_job_id", table_name="credit_ledger_entries")
    op.drop_index("ix_credit_ledger_entries_user_id", table_name="credit_ledger_entries")
    op.drop_table("credit_ledger_entries")

    op.drop_index("ix_credit_voucher_redemptions_user_id", table_name="credit_voucher_redemptions")
    op.drop_index(
        "ix_credit_voucher_redemptions_voucher_id", table_name="credit_voucher_redemptions"
    )
    op.drop_table("credit_voucher_redemptions")

    op.drop_index("ix_credit_vouchers_code_hash", table_name="credit_vouchers")
    op.drop_table("credit_vouchers")

    op.drop_column("render_jobs", "credits_charged")
