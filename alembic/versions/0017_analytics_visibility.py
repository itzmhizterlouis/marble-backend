"""Remember analytics visibility per platform publication without deleting history."""

import sqlalchemy as sa

from alembic import op

revision = "0017_analytics_visibility"
down_revision = "0016_referral_discount"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("publications")}
    for column in (
        sa.Column("analytics_hidden_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("analytics_hidden_reason", sa.String(24), nullable=True),
    ):
        if column.name not in columns:
            op.add_column("publications", column)


def downgrade() -> None:
    op.drop_column("publications", "analytics_hidden_reason")
    op.drop_column("publications", "analytics_hidden_at")
