"""Add durable extraction diagnostics for webpage captures."""

import sqlalchemy as sa
from alembic import op

revision = "0007_webpage_extraction"
down_revision = "0006_voice_transcription"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "captures",
        sa.Column("extraction_attempts", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("captures", "extraction_attempts")
