"""Add durable vision diagnostics for image captures."""

import sqlalchemy as sa
from alembic import op

revision = "0008_image_vision"
down_revision = "0007_webpage_extraction"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "captures",
        sa.Column("vision_attempts", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("captures", "vision_attempts")
