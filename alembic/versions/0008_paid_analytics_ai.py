"""Add billing, entitlement, analytics, and AI records."""

from datetime import UTC, datetime, timedelta
import uuid

import sqlalchemy as sa
from alembic import op

revision = "0008_paid_analytics_ai"
down_revision = "0007_connection_handles"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The original baseline migration intentionally creates current metadata
    # for brand-new databases. Existing production databases are already at
    # 0007 and need the explicit DDL below; fresh databases already have it.
    if "subscriptions" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "subscriptions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("plan", sa.String(16), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("reference", sa.String(255), nullable=True, unique=True),
        sa.Column("paystack_customer_code", sa.String(255), nullable=True),
        sa.Column("paystack_subscription_code", sa.String(255), nullable=True),
        sa.Column("paystack_email_token", sa.String(255), nullable=True),
        sa.Column("amount_kobo", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(8), nullable=False),
        sa.Column("paid_through", sa.DateTime(timezone=True), nullable=True),
        sa.Column("grace_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_at_period_end", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_subscriptions_user_id", "subscriptions", ["user_id"])
    op.create_index("ix_subscriptions_status", "subscriptions", ["status"])
    op.create_index("ix_subscriptions_paid_through", "subscriptions", ["paid_through"])
    op.create_index("ix_subscriptions_reference", "subscriptions", ["reference"], unique=True)
    op.create_index("ix_subscriptions_paystack_subscription_code", "subscriptions", ["paystack_subscription_code"])

    op.create_table(
        "billing_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("provider_event_id", sa.String(64), nullable=False, unique=True),
        sa.Column("event_type", sa.String(80), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_billing_events_provider_event_id", "billing_events", ["provider_event_id"], unique=True)
    op.create_index("ix_billing_events_event_type", "billing_events", ["event_type"])

    op.create_table(
        "complimentary_grants",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("plan", sa.String(16), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("granted_by_user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_complimentary_grants_user_id", "complimentary_grants", ["user_id"])
    op.create_index("ix_complimentary_grants_starts_at", "complimentary_grants", ["starts_at"])
    op.create_index("ix_complimentary_grants_ends_at", "complimentary_grants", ["ends_at"])

    op.create_table(
        "trial_usage",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True),
        sa.Column("post_id", sa.String(36), sa.ForeignKey("posts.id", ondelete="SET NULL"), nullable=True),
        sa.Column("provider_account_ids", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("reserved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_trial_usage_user_id", "trial_usage", ["user_id"], unique=True)

    op.create_table(
        "entitlement_audit_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("actor_user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_entitlement_audit_events_user_id", "entitlement_audit_events", ["user_id"])
    op.create_index("ix_entitlement_audit_events_action", "entitlement_audit_events", ["action"])

    op.create_table(
        "feature_flags",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("updated_by_user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "account_analytics_snapshots",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("platform", sa.String(24), nullable=False),
        sa.Column("raw_metrics", sa.JSON(), nullable=False),
        sa.Column("normalized_metrics", sa.JSON(), nullable=False),
        sa.Column("primary_metric", sa.String(64), nullable=True),
        sa.Column("primary_label", sa.String(80), nullable=True),
        sa.Column("provider_status", sa.String(24), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_account_analytics_user_platform_captured", "account_analytics_snapshots", ["user_id", "platform", "captured_at"])

    op.create_table(
        "publication_metric_snapshots",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("publication_id", sa.String(36), sa.ForeignKey("publications.id", ondelete="CASCADE"), nullable=False),
        sa.Column("raw_metrics", sa.JSON(), nullable=False),
        sa.Column("normalized_metrics", sa.JSON(), nullable=False),
        sa.Column("primary_metric", sa.String(64), nullable=True),
        sa.Column("primary_label", sa.String(80), nullable=True),
        sa.Column("provider_status", sa.String(24), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_publication_metrics_publication_captured", "publication_metric_snapshots", ["publication_id", "captured_at"])

    op.create_table(
        "generated_insights",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("period_days", sa.Integer(), nullable=False),
        sa.Column("source_fingerprint", sa.String(64), nullable=False),
        sa.Column("summary", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", "period_days", name="uq_insight_user_period"),
    )
    op.create_index("ix_generated_insights_user_id", "generated_insights", ["user_id"])

    op.create_table(
        "ai_usage_counters",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("usage_date", sa.String(10), nullable=False),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", "usage_date", "kind", name="uq_ai_usage_day_kind"),
    )
    op.create_index("ix_ai_usage_counters_user_id", "ai_usage_counters", ["user_id"])
    op.create_index("ix_ai_usage_counters_usage_date", "ai_usage_counters", ["usage_date"])

    op.create_table(
        "ai_generation_jobs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("post_id", sa.String(36), sa.ForeignKey("posts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("parent_job_id", sa.String(36), sa.ForeignKey("ai_generation_jobs.id", ondelete="SET NULL"), nullable=True),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("adjustment", sa.String(32), nullable=True),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("model", sa.String(80), nullable=False),
        sa.Column("candidate", sa.JSON(), nullable=True),
        sa.Column("error_code", sa.String(80), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_ai_generation_jobs_user_id", "ai_generation_jobs", ["user_id"])
    op.create_index("ix_ai_generation_jobs_post_id", "ai_generation_jobs", ["post_id"])
    op.create_index("ix_ai_generation_jobs_status", "ai_generation_jobs", ["status"])

    now = datetime.now(UTC)
    connection = op.get_bind()
    verified_users = connection.execute(sa.text("SELECT id FROM users WHERE email_verified = true")).fetchall()
    for (user_id,) in verified_users:
        connection.execute(
            sa.text(
                "INSERT INTO complimentary_grants "
                "(id, user_id, plan, starts_at, ends_at, reason, granted_by_user_id, created_at, updated_at) "
                "VALUES (:id, :user_id, 'basic', :starts_at, :ends_at, :reason, NULL, :now, :now)"
            ),
            {
                "id": str(uuid.uuid4()),
                "user_id": user_id,
                "starts_at": now,
                "ends_at": now + timedelta(days=30),
                "reason": "Launch complimentary month for existing verified creator",
                "now": now,
            },
        )
        post_id = connection.execute(
            sa.text(
                "SELECT posts.id FROM posts JOIN publications ON publications.post_id = posts.id "
                "WHERE posts.user_id = :user_id ORDER BY posts.created_at LIMIT 1"
            ),
            {"user_id": user_id},
        ).scalar()
        if post_id:
            statement = sa.text(
                    "INSERT INTO trial_usage "
                    "(id, user_id, post_id, provider_account_ids, status, reserved_at, consumed_at, created_at, updated_at) "
                    "VALUES (:id, :user_id, :post_id, :accounts, 'consumed', :now, :now, :now, :now)"
                ).bindparams(sa.bindparam("accounts", type_=sa.JSON()))
            connection.execute(
                statement,
                {"id": str(uuid.uuid4()), "user_id": user_id, "post_id": post_id, "accounts": [], "now": now},
            )


def downgrade() -> None:
    for table in (
        "ai_generation_jobs",
        "ai_usage_counters",
        "generated_insights",
        "publication_metric_snapshots",
        "account_analytics_snapshots",
        "feature_flags",
        "entitlement_audit_events",
        "trial_usage",
        "complimentary_grants",
        "billing_events",
        "subscriptions",
    ):
        op.drop_table(table)
