"""Store shared-caption linkage as an explicit typed field."""

import sqlalchemy as sa

from alembic import op

revision = "0013_explicit_caption_linkage"
down_revision = "0012_caption_content_contract"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("platform_versions")
    }
    if "use_shared_caption" not in columns:
        op.add_column(
            "platform_versions",
            sa.Column("use_shared_caption", sa.Boolean(), nullable=True),
        )


def downgrade() -> None:
    columns = {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("platform_versions")
    }
    if "use_shared_caption" in columns:
        op.drop_column("platform_versions", "use_shared_caption")
