"""Database lifecycle helpers. Web deployments select PostgreSQL by configuration."""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Engine, create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker


class Database:
    def __init__(self, url: str) -> None:
        connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
        if url.startswith("sqlite"):
            database_path = make_url(url).database
            if database_path and database_path != ":memory:":
                Path(database_path).parent.mkdir(parents=True, exist_ok=True)
        self.engine: Engine = create_engine(url, connect_args=connect_args)
        self._sessions = sessionmaker(self.engine, expire_on_commit=False)

    def create_schema(self) -> None:
        from quapylab.persistence.models import Base

        Base.metadata.create_all(self.engine)
        # Development/desktop historically bootstrapped with create_all rather than
        # Alembic. Keep those existing databases startable for additive lifecycle
        # columns; supported deployments still run the versioned migration.
        inspector = inspect(self.engine)
        with self.engine.begin() as connection:
            for table in ("groups", "projects", "workflows", "datasets"):
                columns = {column["name"] for column in inspector.get_columns(table)}
                if "deleted_at" not in columns:
                    connection.execute(text(f"ALTER TABLE {table} ADD COLUMN deleted_at TIMESTAMP"))
                connection.execute(
                    text(
                        f"CREATE INDEX IF NOT EXISTS ix_{table}_deleted_at ON {table} (deleted_at)"
                    )
                )

    @contextmanager
    def transaction(self) -> Iterator[Session]:
        session = self._sessions()
        try:
            with session.begin():
                yield session
        finally:
            session.close()
