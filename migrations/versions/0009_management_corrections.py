"""Add completion state and an audit trail for user corrections."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0009_management_corrections"
down_revision = "0008_image_vision"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "captures",
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "capture_corrections",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("capture_id", sa.Uuid(), nullable=False),
        sa.Column("field", sa.String(length=64), nullable=False),
        sa.Column("old_value", postgresql.JSONB(), nullable=True),
        sa.Column("new_value", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["capture_id"],
            ["captures.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_capture_corrections_capture_created_at",
        "capture_corrections",
        ["capture_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_capture_corrections_capture_created_at",
        table_name="capture_corrections",
    )
    op.drop_table("capture_corrections")
    op.drop_column("captures", "completed_at")
