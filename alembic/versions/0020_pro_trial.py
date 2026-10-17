"""Replace the one-post preview with a seven-day Pro trial.

Verified creators who never used their one-post preview get a fresh seven-day
trial from deployment. Creators who already used the preview keep managing
that post through trial_usage but receive no new trial.
"""
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa

from alembic import op

revision = "0020_pro_trial"
down_revision = "0019_ai_adjustments"
branch_labels = None
depends_on = None


def upgrade():
    columns = {item["name"] for item in sa.inspect(op.get_bind()).get_columns("users")}
    if "trial_ends_at" not in columns:
        op.add_column("users", sa.Column("trial_ends_at", sa.DateTime(timezone=True), nullable=True))
    op.get_bind().execute(
        sa.text(
            "UPDATE users SET trial_ends_at = :ends_at "
            "WHERE email_verified = :verified AND trial_ends_at IS NULL "
            "AND NOT EXISTS (SELECT 1 FROM trial_usage WHERE trial_usage.user_id = users.id)"
        ),
        {"ends_at": datetime.now(UTC) + timedelta(days=7), "verified": True},
    )


def downgrade():
    op.drop_column("users", "trial_ends_at")
