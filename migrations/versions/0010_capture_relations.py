"""Store conservative related-memory candidates and user feedback."""

import sqlalchemy as sa
from alembic import op

revision = "0010_capture_relations"
down_revision = "0009_management_corrections"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "capture_relations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("source_capture_id", sa.Uuid(), nullable=False),
        sa.Column("target_capture_id", sa.Uuid(), nullable=False),
        sa.Column("relationship", sa.String(length=32), nullable=False, server_default="related"),
        sa.Column("similarity", sa.Float(), nullable=False),
        sa.Column("explanation", sa.Text(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("feedback", sa.String(length=16), nullable=True),
        sa.Column("notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(["source_capture_id"], ["captures.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["target_capture_id"], ["captures.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source_capture_id",
            "target_capture_id",
            name="uq_capture_relations_source_target",
        ),
    )
    op.create_index(
        "ix_capture_relations_source_active_similarity",
        "capture_relations",
        ["source_capture_id", "active", "similarity"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_capture_relations_source_active_similarity",
        table_name="capture_relations",
    )
    op.drop_table("capture_relations")
