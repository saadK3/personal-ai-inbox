"""Add derived text, metadata, embeddings, and processing diagnostics."""

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision = "0005_capture_enrichment"
down_revision = "0004_generic_channel_ids"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("captures", sa.Column("processing_error", sa.Text(), nullable=True))
    op.add_column(
        "captures",
        sa.Column("enrichment_attempts", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("captures", sa.Column("normalized_text", sa.Text(), nullable=True))
    op.add_column("captures", sa.Column("summary", sa.Text(), nullable=True))
    op.add_column("captures", sa.Column("inferred_type", sa.String(length=64), nullable=True))
    op.add_column(
        "captures",
        sa.Column(
            "topics",
            postgresql.JSONB(),
            nullable=True,
        ),
    )
    op.add_column(
        "captures",
        sa.Column(
            "entities",
            postgresql.JSONB(),
            nullable=True,
        ),
    )
    op.add_column("captures", sa.Column("embedding", Vector(1536), nullable=True))
    op.add_column("captures", sa.Column("embedding_model", sa.String(length=128), nullable=True))
    op.add_column(
        "captures",
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_captures_processing_status",
        "captures",
        ["processing_status", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_captures_processing_status", table_name="captures")
    op.drop_column("captures", "processed_at")
    op.drop_column("captures", "embedding_model")
    op.drop_column("captures", "embedding")
    op.drop_column("captures", "entities")
    op.drop_column("captures", "topics")
    op.drop_column("captures", "inferred_type")
    op.drop_column("captures", "summary")
    op.drop_column("captures", "normalized_text")
    op.drop_column("captures", "enrichment_attempts")
    op.drop_column("captures", "processing_error")
