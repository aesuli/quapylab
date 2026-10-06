"""Initial persistence schema.

Revision ID: 20261006_01
Revises:
Create Date: 2026-10-06
"""

from alembic import op

from quapylab.persistence.models import Base

revision = "20261006_01"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    Base.metadata.create_all(bind=op.get_bind())


def downgrade() -> None:
    Base.metadata.drop_all(bind=op.get_bind())
