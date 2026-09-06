import uuid
from datetime import UTC, datetime

from sqlalchemy import JSON, BigInteger, DateTime, Index, String, Text, UniqueConstraint, Uuid, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Capture(Base):
    """An immutable text submission received from a supported chat channel."""

    __tablename__ = "captures"
    __table_args__ = (
        UniqueConstraint(
            "platform",
            "external_message_id",
            name="uq_captures_platform_external_message_id",
        ),
        Index(
            "ix_captures_platform_conversation_created_at",
            "platform",
            "conversation_id",
            "created_at",
        ),
        Index("ix_captures_active_created_at", "deleted_at", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    platform: Mapped[str] = mapped_column(String(32), nullable=False, default="discord")
    external_message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    conversation_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sender_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False, default="text")
    raw_text: Mapped[str] = mapped_column(Text, nullable=False)
    source_metadata: Mapped[dict] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"),
        nullable=False,
        default=dict,
    )
    processing_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="captured", server_default="captured"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class MessageReceipt(Base):
    """Durable receipt used to make every authorized channel message idempotent."""

    __tablename__ = "message_receipts"

    platform: Mapped[str] = mapped_column(String(32), primary_key=True)
    external_message_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    conversation_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sender_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )
