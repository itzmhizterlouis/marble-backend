"""Track connection activity for the customer trust centre."""

import sqlalchemy as sa

from alembic import op

revision = "0003_connection_trust_metadata"
down_revision = "0002_publication_attempts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("social_connections")}
    if "connected_at" not in columns:
        op.add_column(
            "social_connections",
            sa.Column("connected_at", sa.DateTime(timezone=True), nullable=True),
        )
    if "last_used_at" not in columns:
        op.add_column(
            "social_connections",
            sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        )


def downgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("social_connections")}
    if "last_used_at" in columns:
        op.drop_column("social_connections", "last_used_at")
    if "connected_at" in columns:
        op.drop_column("social_connections", "connected_at")
