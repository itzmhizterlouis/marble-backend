"""Add creator referrals, commission ledger and bank withdrawals."""

import secrets

import sqlalchemy as sa

from alembic import op

revision = "0014_affiliate_referrals"
down_revision = "0013_explicit_caption_linkage"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("users")}
    if "referral_code" not in columns:
        op.add_column("users", sa.Column("referral_code", sa.String(20), nullable=True))
        op.create_index("ix_users_referral_code", "users", ["referral_code"], unique=True)
    if "referred_by_user_id" not in columns:
        op.add_column("users", sa.Column("referred_by_user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True))
        op.create_index("ix_users_referred_by_user_id", "users", ["referred_by_user_id"])
    if "first_paid_reference" not in columns:
        op.add_column("users", sa.Column("first_paid_reference", sa.String(255), nullable=True))

    users = sa.table("users", sa.column("id", sa.String), sa.column("referral_code", sa.String))
    for (user_id,) in bind.execute(sa.text("SELECT id FROM users WHERE referral_code IS NULL")):
        bind.execute(users.update().where(users.c.id == user_id).values(referral_code=secrets.token_hex(8).upper()))
    # Existing accounts cannot be referred retroactively. Keep a durable marker
    # for their earlier successful payment where a reference is available.
    bind.execute(sa.text("""
        UPDATE users SET first_paid_reference = (
            SELECT s.reference FROM subscriptions s
            WHERE s.user_id = users.id AND s.reference IS NOT NULL
              AND s.status IN ('active', 'grace', 'cancelled', 'replaced')
            ORDER BY s.created_at ASC LIMIT 1
        ) WHERE first_paid_reference IS NULL
    """))

    if "affiliate_commissions" not in inspector.get_table_names():
        op.create_table(
            "affiliate_commissions",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("referrer_user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
            sa.Column("referred_user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, unique=True),
            sa.Column("payment_reference", sa.String(255), nullable=False, unique=True),
            sa.Column("subscription_id", sa.String(36), sa.ForeignKey("subscriptions.id", ondelete="SET NULL"), nullable=True),
            sa.Column("payment_amount_kobo", sa.BigInteger(), nullable=False),
            sa.Column("amount_kobo", sa.BigInteger(), nullable=False),
            sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("reversed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("reversal_reason", sa.String(80), nullable=True),
            sa.Column("requires_review", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_affiliate_commissions_referrer_user_id", "affiliate_commissions", ["referrer_user_id"])
        op.create_index("ix_affiliate_commissions_referred_user_id", "affiliate_commissions", ["referred_user_id"], unique=True)
        op.create_index("ix_affiliate_commissions_available_at", "affiliate_commissions", ["available_at"])

    if "affiliate_bank_recipients" not in inspector.get_table_names():
        op.create_table(
            "affiliate_bank_recipients",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True),
            sa.Column("recipient_code", sa.String(255), nullable=False),
            sa.Column("bank_code", sa.String(32), nullable=False),
            sa.Column("bank_name", sa.String(255), nullable=False),
            sa.Column("account_name", sa.String(255), nullable=False),
            sa.Column("account_last_four", sa.String(4), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )

    if "affiliate_payment_reversals" not in inspector.get_table_names():
        op.create_table(
            "affiliate_payment_reversals",
            sa.Column("payment_reference", sa.String(255), primary_key=True),
            sa.Column("reason", sa.String(80), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )

    if "affiliate_payouts" not in inspector.get_table_names():
        op.create_table(
            "affiliate_payouts",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
            sa.Column("recipient_code", sa.String(255), nullable=False),
            sa.Column("bank_name", sa.String(255), nullable=False),
            sa.Column("account_name", sa.String(255), nullable=False),
            sa.Column("account_last_four", sa.String(4), nullable=False),
            sa.Column("requested_kobo", sa.BigInteger(), nullable=False),
            sa.Column("fee_kobo", sa.BigInteger(), nullable=False),
            sa.Column("net_kobo", sa.BigInteger(), nullable=False),
            sa.Column("status", sa.String(32), nullable=False),
            sa.Column("idempotency_key", sa.String(255), nullable=False),
            sa.Column("transfer_reference", sa.String(50), nullable=False, unique=True),
            sa.Column("transfer_code", sa.String(255), nullable=True),
            sa.Column("approved_by_user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
            sa.Column("decision_reason", sa.Text(), nullable=True),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("user_id", "idempotency_key", name="uq_affiliate_payout_user_key"),
        )
        op.create_index("ix_affiliate_payouts_user_id", "affiliate_payouts", ["user_id"])
        op.create_index("ix_affiliate_payouts_status", "affiliate_payouts", ["status"])

    if "affiliate_ledger_entries" not in inspector.get_table_names():
        op.create_table(
            "affiliate_ledger_entries",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
            sa.Column("commission_id", sa.String(36), sa.ForeignKey("affiliate_commissions.id", ondelete="RESTRICT"), nullable=True),
            sa.Column("payout_id", sa.String(36), sa.ForeignKey("affiliate_payouts.id", ondelete="RESTRICT"), nullable=True),
            sa.Column("kind", sa.String(24), nullable=False),
            sa.Column("amount_kobo", sa.BigInteger(), nullable=False),
            sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_affiliate_ledger_entries_user_id", "affiliate_ledger_entries", ["user_id"])
        op.create_index("ix_affiliate_ledger_entries_available_at", "affiliate_ledger_entries", ["available_at"])

    if "affiliate_audit_events" not in inspector.get_table_names():
        op.create_table(
            "affiliate_audit_events",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
            sa.Column("actor_user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
            sa.Column("payout_id", sa.String(36), sa.ForeignKey("affiliate_payouts.id", ondelete="RESTRICT"), nullable=True),
            sa.Column("action", sa.String(80), nullable=False),
            sa.Column("details", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_affiliate_audit_events_user_id", "affiliate_audit_events", ["user_id"])


def downgrade() -> None:
    for table in ("affiliate_audit_events", "affiliate_ledger_entries", "affiliate_payouts", "affiliate_bank_recipients", "affiliate_payment_reversals", "affiliate_commissions"):
        if table in sa.inspect(op.get_bind()).get_table_names():
            op.drop_table(table)
    for column in ("first_paid_reference", "referred_by_user_id", "referral_code"):
        if column in {item["name"] for item in sa.inspect(op.get_bind()).get_columns("users")}:
            op.drop_column("users", column)
