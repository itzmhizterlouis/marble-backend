"""Persist grounded video observations for text-only AI variants."""

import sqlalchemy as sa

from alembic import op

revision = "0015_ai_video_observations"
down_revision = "0014_affiliate_referrals"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("ai_generation_jobs")}
    if "video_observations" not in columns:
        op.add_column("ai_generation_jobs", sa.Column("video_observations", sa.JSON(), nullable=True))


def downgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("ai_generation_jobs")}
    if "video_observations" in columns:
        op.drop_column("ai_generation_jobs", "video_observations")
