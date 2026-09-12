"""Associate AI generations with the source video they reviewed."""

import sqlalchemy as sa
from alembic import op


revision = "0010_ai_generation_media"
down_revision = "0009_ai_generation_context"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "ai_generation_jobs" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("ai_generation_jobs")}
    if "media_id" not in columns:
        op.add_column(
            "ai_generation_jobs",
            sa.Column(
                "media_id",
                sa.String(36),
                sa.ForeignKey("media_assets.id", ondelete="SET NULL"),
                nullable=True,
            ),
        )
    indexes = {index["name"] for index in inspector.get_indexes("ai_generation_jobs")}
    if "ix_ai_generation_jobs_media_id" not in indexes:
        op.create_index("ix_ai_generation_jobs_media_id", "ai_generation_jobs", ["media_id"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "ai_generation_jobs" not in inspector.get_table_names():
        return
    indexes = {index["name"] for index in inspector.get_indexes("ai_generation_jobs")}
    if "ix_ai_generation_jobs_media_id" in indexes:
        op.drop_index("ix_ai_generation_jobs_media_id", table_name="ai_generation_jobs")
    columns = {column["name"] for column in inspector.get_columns("ai_generation_jobs")}
    if "media_id" in columns:
        op.drop_column("ai_generation_jobs", "media_id")
