"""Add reversible lifecycle deletion timestamps.

Revision ID: 20261007_03
Revises: 20261006_02
Create Date: 2026-10-07
"""

import sqlalchemy as sa
from alembic import op

revision = "20261007_03"
down_revision = "20261006_02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in ("groups", "projects", "workflows", "datasets"):
        inspector = sa.inspect(op.get_bind())
        columns = {column["name"] for column in inspector.get_columns(table)}
        indexes = {index["name"] for index in inspector.get_indexes(table)}
        if "deleted_at" not in columns:
            op.add_column(table, sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
        if f"ix_{table}_deleted_at" not in indexes:
            op.create_index(f"ix_{table}_deleted_at", table, ["deleted_at"])


def downgrade() -> None:
    for table in ("datasets", "workflows", "projects", "groups"):
        op.drop_index(f"ix_{table}_deleted_at", table_name=table)
        op.drop_column(table, "deleted_at")
