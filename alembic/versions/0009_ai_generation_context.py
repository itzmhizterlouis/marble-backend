"""Store creator context used for AI generation."""

import sqlalchemy as sa
from alembic import op

revision = "0009_ai_generation_context"
down_revision = "0008_paid_analytics_ai"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = inspector.get_table_names()
    if "ai_generation_jobs" not in tables:
        return
    columns = {column["name"] for column in inspector.get_columns("ai_generation_jobs")}
    if "generation_context" not in columns:
        op.add_column(
            "ai_generation_jobs",
            sa.Column("generation_context", sa.Text(), nullable=True),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "ai_generation_jobs" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("ai_generation_jobs")}
    if "generation_context" in columns:
        op.drop_column("ai_generation_jobs", "generation_context")
