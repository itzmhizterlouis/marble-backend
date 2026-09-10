"""Add R2 object metadata and resumable multipart upload state."""

import sqlalchemy as sa

from alembic import op

revision = "0005_r2_media_storage"
down_revision = "0004_post_notifications"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("media_assets")}
    if "storage_backend" not in columns:
        op.add_column(
            "media_assets",
            sa.Column("storage_backend", sa.String(length=16), nullable=False, server_default="local"),
        )
        op.create_index("ix_media_assets_storage_backend", "media_assets", ["storage_backend"])
    if "object_key" not in columns:
        op.add_column("media_assets", sa.Column("object_key", sa.Text(), nullable=True))
    if "thumbnail_key" not in columns:
        op.add_column("media_assets", sa.Column("thumbnail_key", sa.Text(), nullable=True))
    if "multipart_upload_id" not in columns:
        op.add_column("media_assets", sa.Column("multipart_upload_id", sa.String(length=255), nullable=True))

    if "media_upload_parts" not in inspector.get_table_names():
        op.create_table(
            "media_upload_parts",
            sa.Column("media_id", sa.String(length=36), nullable=False),
            sa.Column("part_number", sa.Integer(), nullable=False),
            sa.Column("etag", sa.String(length=255), nullable=False),
            sa.Column("size_bytes", sa.BigInteger(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["media_id"], ["media_assets.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("media_id", "part_number"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "media_upload_parts" in inspector.get_table_names():
        op.drop_table("media_upload_parts")
    columns = {column["name"] for column in inspector.get_columns("media_assets")}
    if "multipart_upload_id" in columns:
        op.drop_column("media_assets", "multipart_upload_id")
    if "thumbnail_key" in columns:
        op.drop_column("media_assets", "thumbnail_key")
    if "object_key" in columns:
        op.drop_column("media_assets", "object_key")
    if "storage_backend" in columns:
        if "ix_media_assets_storage_backend" in {
            index["name"] for index in inspector.get_indexes("media_assets")
        }:
            op.drop_index("ix_media_assets_storage_backend", table_name="media_assets")
        op.drop_column("media_assets", "storage_backend")
