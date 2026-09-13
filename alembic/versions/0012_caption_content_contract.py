"""Add canonical caption format and submitted-content snapshots."""

import sqlalchemy as sa

from alembic import op

revision = "0012_caption_content_contract"
down_revision = "0011_analytics_period_snapshots"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    post_columns = {column["name"] for column in inspector.get_columns("posts")}
    if "content_format_version" not in post_columns:
        op.add_column(
            "posts",
            sa.Column("content_format_version", sa.Integer(), nullable=False, server_default="1"),
        )

    publication_columns = {
        column["name"] for column in inspector.get_columns("publications")
    }
    if "submitted_caption" not in publication_columns:
        op.add_column("publications", sa.Column("submitted_caption", sa.Text(), nullable=True))
    if "submitted_title" not in publication_columns:
        op.add_column(
            "publications", sa.Column("submitted_title", sa.String(255), nullable=True)
        )
    if "submitted_hashtags" not in publication_columns:
        op.add_column("publications", sa.Column("submitted_hashtags", sa.JSON(), nullable=True))


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    publication_columns = {
        column["name"] for column in inspector.get_columns("publications")
    }
    for column in ("submitted_hashtags", "submitted_title", "submitted_caption"):
        if column in publication_columns:
            op.drop_column("publications", column)
    post_columns = {column["name"] for column in inspector.get_columns("posts")}
    if "content_format_version" in post_columns:
        op.drop_column("posts", "content_format_version")
