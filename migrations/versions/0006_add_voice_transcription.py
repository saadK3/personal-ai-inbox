"""Add durable transcription fields for voice-note captures."""

import sqlalchemy as sa
from alembic import op

revision = "0006_voice_transcription"
down_revision = "0005_capture_enrichment"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "captures",
        sa.Column("transcription_attempts", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("captures", sa.Column("raw_transcription", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("captures", "raw_transcription")
    op.drop_column("captures", "transcription_attempts")
