"""Immutable subtitle video versions and explicit draft selection."""

import sqlalchemy as sa

from alembic import op
from app.models import SubtitleJob

revision = "0018_auto_subtitles"
down_revision = "0017_analytics_visibility"
branch_labels = None
depends_on = None


def upgrade():
    SubtitleJob.__table__.create(op.get_bind(), checkfirst=True)
    columns = {item["name"] for item in sa.inspect(op.get_bind()).get_columns("posts")}
    if "subtitle_job_id" not in columns:
        with op.batch_alter_table("posts") as batch:
            batch.add_column(sa.Column("subtitle_job_id", sa.String(36), nullable=True))
            batch.create_foreign_key(
                "fk_post_subtitle", "subtitle_jobs", ["subtitle_job_id"], ["id"], ondelete="RESTRICT"
            )


def downgrade():
    with op.batch_alter_table("posts") as batch:
        # Fresh databases are created by the legacy initial metadata migration.
        for fk in sa.inspect(op.get_bind()).get_foreign_keys("posts"):
            if fk["constrained_columns"] == ["subtitle_job_id"] and fk.get("name"):
                batch.drop_constraint(fk["name"], type_="foreignkey")
        batch.drop_column("subtitle_job_id")
    op.drop_table("subtitle_jobs")
