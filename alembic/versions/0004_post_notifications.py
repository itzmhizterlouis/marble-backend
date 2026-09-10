"""Add a transactional outbox for post failure emails."""

import sqlalchemy as sa

from alembic import op

revision = "0004_post_notifications"
down_revision = "0003_connection_trust_metadata"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "post_notifications" in inspector.get_table_names():
        return
    op.create_table(
        "post_notifications",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("post_id", sa.String(length=36), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("publish_revision", sa.Integer(), nullable=False),
        sa.Column("dedupe_key", sa.String(length=255), nullable=False),
        sa.Column("recipient_email", sa.String(length=320), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("provider_message_id", sa.String(length=255), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["post_id"], ["posts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_post_notifications_post_id", "post_notifications", ["post_id"])
    op.create_index("ix_post_notifications_dedupe_key", "post_notifications", ["dedupe_key"], unique=True)
    op.create_index("ix_post_notifications_status", "post_notifications", ["status"])
    op.create_index("ix_post_notifications_next_attempt_at", "post_notifications", ["next_attempt_at"])


def downgrade() -> None:
    if "post_notifications" in sa.inspect(op.get_bind()).get_table_names():
        op.drop_table("post_notifications")
