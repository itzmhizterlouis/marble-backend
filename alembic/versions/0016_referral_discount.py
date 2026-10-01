"""Persist one-time referral checkout and the original-price commission basis."""

import sqlalchemy as sa

from alembic import op

revision = "0016_referral_discount"
down_revision = "0015_ai_video_observations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("affiliate_commissions", sa.Column("commission_base_kobo", sa.BigInteger(), nullable=True))
    op.execute("UPDATE affiliate_commissions SET commission_base_kobo = payment_amount_kobo")
    op.create_table(
        "referral_checkouts",
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("reference", sa.String(255), nullable=False, unique=True),
        sa.Column("plan", sa.String(16), nullable=False),
        sa.Column("original_amount_kobo", sa.BigInteger(), nullable=False),
        sa.Column("amount_kobo", sa.BigInteger(), nullable=False),
        sa.Column("authorization_url", sa.Text(), nullable=True),
        sa.Column("subscription_id", sa.String(36), sa.ForeignKey("subscriptions.id", ondelete="SET NULL"), nullable=True),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("customer_id", sa.String(64), nullable=True),
        sa.Column("authorization_code", sa.String(255), nullable=True),
        sa.Column("renewal_start_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("renewal_state", sa.String(24), nullable=False),
        sa.Column("renewal_attempted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_referral_checkouts_renewal_state", "referral_checkouts", ["renewal_state"])


def downgrade() -> None:
    op.drop_table("referral_checkouts")
    op.drop_column("affiliate_commissions", "commission_base_kobo")
