"""Preserve provider identifiers for every publication attempt."""

import sqlalchemy as sa

from alembic import op

revision = "0002_publication_attempts"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "publication_attempts" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "publication_attempts",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("publication_id", sa.String(length=36), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("provider_request_id", sa.String(length=255), nullable=True),
        sa.Column("provider_job_id", sa.String(length=255), nullable=True),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["publication_id"], ["publications.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "publication_id", "idempotency_key", name="uq_attempt_publication_key"
        ),
    )
    op.create_index(
        op.f("ix_publication_attempts_publication_id"),
        "publication_attempts",
        ["publication_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_publication_attempts_provider_request_id"),
        "publication_attempts",
        ["provider_request_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_publication_attempts_provider_job_id"),
        "publication_attempts",
        ["provider_job_id"],
        unique=False,
    )


def downgrade() -> None:
    if "publication_attempts" not in sa.inspect(op.get_bind()).get_table_names():
        return
    op.drop_index(
        op.f("ix_publication_attempts_provider_job_id"), table_name="publication_attempts"
    )
    op.drop_index(
        op.f("ix_publication_attempts_provider_request_id"), table_name="publication_attempts"
    )
    op.drop_index(
        op.f("ix_publication_attempts_publication_id"), table_name="publication_attempts"
    )
    op.drop_table("publication_attempts")
