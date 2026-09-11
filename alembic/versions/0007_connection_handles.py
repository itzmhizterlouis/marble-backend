"""Store public social account handles separately from provider identifiers."""

import sqlalchemy as sa

from alembic import op

revision = "0007_connection_handles"
down_revision = "0006_expand_r2_upload_id"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("social_connections")}
    if "handle" not in columns:
        op.add_column(
            "social_connections",
            sa.Column("handle", sa.String(length=255), nullable=True),
        )


def downgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("social_connections")}
    if "handle" in columns:
        op.drop_column("social_connections", "handle")
