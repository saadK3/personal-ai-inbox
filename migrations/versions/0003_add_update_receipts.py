"""Add durable Telegram update receipts for idempotent webhook handling."""

import sqlalchemy as sa
from alembic import op

revision = "0003_add_update_receipts"
down_revision = "0002_create_captures"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "telegram_update_receipts",
        sa.Column("update_id", sa.BigInteger(), nullable=False),
        sa.Column("telegram_chat_id", sa.BigInteger(), nullable=False),
        sa.Column("telegram_message_id", sa.BigInteger(), nullable=False),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("update_id"),
    )


def downgrade() -> None:
    op.drop_table("telegram_update_receipts")
