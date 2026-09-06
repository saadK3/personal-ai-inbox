"""Create the durable captures table for the first vertical slice."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002_create_captures"
down_revision = "0001_enable_pgvector"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "captures",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("telegram_update_id", sa.BigInteger(), nullable=False),
        sa.Column("telegram_chat_id", sa.BigInteger(), nullable=False),
        sa.Column("telegram_message_id", sa.BigInteger(), nullable=False),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=False),
        sa.Column("source_type", sa.String(length=32), nullable=False, server_default="text"),
        sa.Column("raw_text", sa.Text(), nullable=False),
        sa.Column(
            "source_metadata",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "processing_status",
            sa.String(length=32),
            nullable=False,
            server_default="captured",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("telegram_update_id", name="uq_captures_telegram_update_id"),
    )
    op.create_index(
        "ix_captures_chat_created_at",
        "captures",
        ["telegram_chat_id", "created_at"],
    )
    op.create_index(
        "ix_captures_active_created_at",
        "captures",
        ["deleted_at", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_captures_active_created_at", table_name="captures")
    op.drop_index("ix_captures_chat_created_at", table_name="captures")
    op.drop_table("captures")
