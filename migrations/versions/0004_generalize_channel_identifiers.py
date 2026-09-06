"""Generalize Slice 1 storage from Telegram to chat-channel identifiers."""

import sqlalchemy as sa
from alembic import op

revision = "0004_generic_channel_ids"
down_revision = "0003_add_update_receipts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("captures", sa.Column("platform", sa.String(length=32), nullable=True))
    op.add_column("captures", sa.Column("external_message_id", sa.BigInteger(), nullable=True))
    op.add_column("captures", sa.Column("conversation_id", sa.BigInteger(), nullable=True))
    op.add_column("captures", sa.Column("sender_id", sa.BigInteger(), nullable=True))

    # Preserve any pre-Discord captures if this migration is applied to a
    # database that was used with the earlier Telegram implementation.
    op.execute(
        """
        UPDATE captures
        SET platform = 'telegram',
            external_message_id = telegram_update_id,
            conversation_id = telegram_chat_id,
            sender_id = telegram_user_id
        """
    )
    op.alter_column("captures", "platform", nullable=False)
    op.alter_column("captures", "external_message_id", nullable=False)
    op.alter_column("captures", "conversation_id", nullable=False)
    op.alter_column("captures", "sender_id", nullable=False)
    op.create_unique_constraint(
        "uq_captures_platform_external_message_id",
        "captures",
        ["platform", "external_message_id"],
    )
    op.create_index(
        "ix_captures_platform_conversation_created_at",
        "captures",
        ["platform", "conversation_id", "created_at"],
    )

    # The legacy columns remain nullable for backward compatibility with any
    # old rows. New code writes only the channel-neutral columns.
    op.alter_column("captures", "telegram_update_id", nullable=True)
    op.alter_column("captures", "telegram_chat_id", nullable=True)
    op.alter_column("captures", "telegram_message_id", nullable=True)
    op.alter_column("captures", "telegram_user_id", nullable=True)

    op.create_table(
        "message_receipts",
        sa.Column("platform", sa.String(length=32), nullable=False),
        sa.Column("external_message_id", sa.BigInteger(), nullable=False),
        sa.Column("conversation_id", sa.BigInteger(), nullable=False),
        sa.Column("sender_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("platform", "external_message_id"),
    )


def downgrade() -> None:
    op.drop_table("message_receipts")
    op.drop_index(
        "ix_captures_platform_conversation_created_at",
        table_name="captures",
    )
    op.drop_constraint(
        "uq_captures_platform_external_message_id",
        "captures",
        type_="unique",
    )
    op.drop_column("captures", "sender_id")
    op.drop_column("captures", "conversation_id")
    op.drop_column("captures", "external_message_id")
    op.drop_column("captures", "platform")
