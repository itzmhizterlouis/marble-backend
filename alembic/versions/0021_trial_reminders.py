"""Remember which trial end each creator was reminded about."""
import sqlalchemy as sa

from alembic import op

revision = "0021_trial_reminders"
down_revision = "0020_pro_trial"
branch_labels = None
depends_on = None


def upgrade():
    columns = {item["name"] for item in sa.inspect(op.get_bind()).get_columns("users")}
    if "trial_reminder_sent_for" not in columns:
        op.add_column("users", sa.Column("trial_reminder_sent_for", sa.DateTime(timezone=True), nullable=True))


def downgrade():
    op.drop_column("users", "trial_reminder_sent_for")
