"""Track the one-time bulk trial extension and its email."""
import sqlalchemy as sa

from alembic import op

revision = "0022_trial_extension"
down_revision = "0021_trial_reminders"
branch_labels = None
depends_on = None

COLUMNS = ("trial_extended_at", "trial_extension_emailed_at")


def upgrade():
    existing = {item["name"] for item in sa.inspect(op.get_bind()).get_columns("users")}
    for name in COLUMNS:
        if name not in existing:
            op.add_column("users", sa.Column(name, sa.DateTime(timezone=True), nullable=True))


def downgrade():
    for name in COLUMNS:
        op.drop_column("users", name)
