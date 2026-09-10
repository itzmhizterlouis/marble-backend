"""Store opaque R2 multipart upload identifiers without a length limit.

Revision ID: 0006_expand_r2_upload_id
Revises: 0005_r2_media_storage
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0006_expand_r2_upload_id"
down_revision: str | None = "0005_r2_media_storage"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("media_assets") as batch_op:
        batch_op.alter_column(
            "multipart_upload_id",
            existing_type=sa.String(length=255),
            type_=sa.Text(),
            existing_nullable=True,
        )


def downgrade() -> None:
    with op.batch_alter_table("media_assets") as batch_op:
        batch_op.alter_column(
            "multipart_upload_id",
            existing_type=sa.Text(),
            type_=sa.String(length=255),
            existing_nullable=True,
        )
