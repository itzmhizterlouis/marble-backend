"""Private clipping grants, durable projects and imported clips."""
import sqlalchemy as sa

from alembic import op
from app.models import ClippingAccess, ClippingProject, GeneratedClip

revision = "0023_clipping_beta"
down_revision = "0022_trial_extension"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    columns = {item["name"] for item in sa.inspect(bind).get_columns("media_assets")}
    if "purpose" not in columns:
        op.add_column("media_assets", sa.Column("purpose", sa.String(16), nullable=False, server_default="post"))
    if "baked_subtitles" not in columns:
        op.add_column("media_assets", sa.Column("baked_subtitles", sa.Boolean(), nullable=False, server_default=sa.false()))
    for table in (ClippingAccess.__table__, ClippingProject.__table__, GeneratedClip.__table__):
        table.create(bind, checkfirst=True)
    flags = sa.table("feature_flags", sa.column("key"), sa.column("enabled"))
    if not bind.scalar(sa.select(flags.c.key).where(flags.c.key == "clipping")):
        # ORM timestamp columns have Python defaults; this insert supplies them explicitly.
        from datetime import UTC, datetime
        flags = sa.table("feature_flags", sa.column("key"), sa.column("enabled"), sa.column("created_at"), sa.column("updated_at"))
        now = datetime.now(UTC)
        bind.execute(flags.insert().values(key="clipping", enabled=False, created_at=now, updated_at=now))


def downgrade():
    op.drop_table("generated_clips")
    op.drop_table("clipping_projects")
    op.drop_table("clipping_access")
    op.execute("DELETE FROM feature_flags WHERE key = 'clipping'")
    with op.batch_alter_table("media_assets") as batch:
        batch.drop_column("baked_subtitles")
        batch.drop_column("purpose")
