"""Allow a group-scoped artifact record for shared physical content.

Revision ID: 20261006_02
Revises: 20261006_01
Create Date: 2026-10-06
"""

import sqlalchemy as sa
from alembic import op

revision = "20261006_02"
down_revision = "20261006_01"
branch_labels = None
depends_on = None


def _storage_key_constraint_name() -> str | None:
    inspector = sa.inspect(op.get_bind())
    for constraint in inspector.get_unique_constraints("artifacts"):
        if constraint["column_names"] == ["storage_key"]:
            return constraint["name"]
    return None


def upgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        # SQLite gives inline UNIQUE constraints no name, so it cannot drop the
        # historical storage_key constraint in place. Rebuild this small table
        # while preserving its rows and foreign-key shape.
        op.execute(
            """
            CREATE TABLE artifacts_replacement (
                id VARCHAR(36) NOT NULL PRIMARY KEY,
                group_id VARCHAR(36) NOT NULL REFERENCES groups (id),
                producing_run_id VARCHAR(36) REFERENCES runs (id),
                producing_node_run_id VARCHAR(36) REFERENCES node_runs (id),
                kind VARCHAR(64) NOT NULL,
                media_type VARCHAR(128) NOT NULL,
                size_bytes INTEGER NOT NULL,
                sha256 VARCHAR(64) NOT NULL,
                storage_key VARCHAR(512) NOT NULL,
                serialization JSON NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                CONSTRAINT uq_artifact_group_sha256 UNIQUE (group_id, sha256)
            )
            """
        )
        op.execute("INSERT INTO artifacts_replacement SELECT * FROM artifacts")
        op.drop_table("artifacts")
        op.rename_table("artifacts_replacement", "artifacts")
        op.create_index("ix_artifacts_group_id", "artifacts", ["group_id"])
        op.create_index("ix_artifacts_sha256", "artifacts", ["sha256"])
        return
    name = _storage_key_constraint_name()
    if name is not None:
        op.drop_constraint(name, "artifacts", type_="unique")
    op.create_unique_constraint("uq_artifact_group_sha256", "artifacts", ["group_id", "sha256"])


def downgrade() -> None:
    op.create_unique_constraint("uq_artifacts_storage_key", "artifacts", ["storage_key"])
