import uuid
from datetime import UTC, datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    JSON,
    BigInteger,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
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
    processing_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    extraction_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    vision_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    transcription_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    enrichment_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    raw_transcription: Mapped[str | None] = mapped_column(Text, nullable=True)
    normalized_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    inferred_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    topics: Mapped[list[str] | None] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=True
    )
    entities: Mapped[dict[str, Any] | None] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=True
    )
    embedding: Mapped[list[float] | None] = mapped_column(Vector(1536), nullable=True)
    embedding_model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
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


class CaptureCorrection(Base):
    """Audit trail for user edits to derived capture fields."""

    __tablename__ = "capture_corrections"
    __table_args__ = (
        Index("ix_capture_corrections_capture_created_at", "capture_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    capture_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("captures.id", ondelete="CASCADE"),
        nullable=False,
    )
    field: Mapped[str] = mapped_column(String(64), nullable=False)
    old_value: Mapped[Any | None] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"),
        nullable=True,
    )
    new_value: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )


class CaptureRelation(Base):
    """A conservative, user-reviewable connection between two captures."""

    __tablename__ = "capture_relations"
    __table_args__ = (
        UniqueConstraint(
            "source_capture_id",
            "target_capture_id",
            name="uq_capture_relations_source_target",
        ),
        Index(
            "ix_capture_relations_source_active_similarity",
            "source_capture_id",
            "active",
            "similarity",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    source_capture_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("captures.id", ondelete="CASCADE"),
        nullable=False,
    )
    target_capture_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("captures.id", ondelete="CASCADE"),
        nullable=False,
    )
    relationship: Mapped[str] = mapped_column(
        String(32), nullable=False, default="related", server_default="related"
    )
    similarity: Mapped[float] = mapped_column(Float, nullable=False)
    explanation: Mapped[str] = mapped_column(Text, nullable=False)
    active: Mapped[bool] = mapped_column(
        nullable=False, default=True, server_default="true"
    )
    feedback: Mapped[str | None] = mapped_column(String(16), nullable=True)
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )
