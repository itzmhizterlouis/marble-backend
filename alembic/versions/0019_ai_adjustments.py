"""Persist combined AI tone and length choices; retain legacy single choices."""
import sqlalchemy as sa

from alembic import op

revision = "0019_ai_adjustments"
down_revision = "0018_auto_subtitles"
branch_labels = None
depends_on = None


def upgrade():
    columns = {item["name"] for item in sa.inspect(op.get_bind()).get_columns("ai_generation_jobs")}
    if "adjustments" not in columns:
        op.add_column("ai_generation_jobs", sa.Column("adjustments", sa.JSON(), nullable=True))


def downgrade():
    op.drop_column("ai_generation_jobs", "adjustments")
