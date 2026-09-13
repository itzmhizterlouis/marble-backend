"""Persist provider date-window analytics for accurate Insights trends."""

import sqlalchemy as sa
from alembic import op

revision = "0011_analytics_period_snapshots"
down_revision = "0010_ai_generation_media"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The baseline migration creates current metadata for brand-new databases.
    # Existing production databases reach this revision without the table.
    if "analytics_period_snapshots" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "analytics_period_snapshots",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("period_days", sa.Integer(), nullable=False),
        sa.Column("start_date", sa.String(10), nullable=False),
        sa.Column("end_date", sa.String(10), nullable=False),
        sa.Column("raw_metrics", sa.JSON(), nullable=False),
        sa.Column("normalized_metrics", sa.JSON(), nullable=False),
        sa.Column("provider_status", sa.String(24), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_analytics_period_user_period_captured",
        "analytics_period_snapshots",
        ["user_id", "period_days", "captured_at"],
    )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "analytics_period_snapshots" not in inspector.get_table_names():
        return
    index_names = {
        item["name"] for item in inspector.get_indexes("analytics_period_snapshots")
    }
    if "ix_analytics_period_user_period_captured" in index_names:
        op.drop_index(
            "ix_analytics_period_user_period_captured",
            table_name="analytics_period_snapshots",
        )
    op.drop_table("analytics_period_snapshots")
