"""Explicit runtime configuration shared by every launch mode."""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

DEFAULT_DATABASE_URL = "sqlite+pysqlite:///./.quapylab/quapylab.sqlite3"


class DeploymentMode(StrEnum):
    DESKTOP = "desktop"
    WEB = "web"


@dataclass(frozen=True, slots=True)
class ApplicationConfig:
    mode: DeploymentMode = DeploymentMode.DESKTOP
    database_url: str = DEFAULT_DATABASE_URL

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any]) -> "ApplicationConfig":
        raw_mode = values.get("QUAPYLAB_MODE", DeploymentMode.DESKTOP)
        try:
            mode = DeploymentMode(raw_mode)
        except ValueError as error:
            choices = ", ".join(item.value for item in DeploymentMode)
            raise ValueError(f"QUAPYLAB_MODE must be one of: {choices}.") from error
        database_url = values.get("QUAPYLAB_DATABASE_URL", DEFAULT_DATABASE_URL)
        if not isinstance(database_url, str) or not database_url:
            raise ValueError("QUAPYLAB_DATABASE_URL must be a non-empty SQLAlchemy URL.")
        return cls(mode=mode, database_url=database_url)
