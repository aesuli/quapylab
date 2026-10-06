from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect


def test_initial_migration_creates_the_persistence_schema(tmp_path: Path) -> None:
    path = tmp_path / "quapylab.sqlite3"
    configuration = Config("alembic.ini")
    configuration.set_main_option("sqlalchemy.url", f"sqlite:///{path}")

    command.upgrade(configuration, "head")

    inspector = inspect(create_engine(f"sqlite:///{path}"))
    assert {"users", "groups", "projects", "workflow_revisions", "artifacts"} <= set(
        inspector.get_table_names()
    )
