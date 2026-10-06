"""Explicit runtime configuration shared by every launch mode."""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class DeploymentMode(StrEnum):
    DESKTOP = "desktop"
    WEB = "web"


@dataclass(frozen=True, slots=True)
class ApplicationConfig:
    mode: DeploymentMode = DeploymentMode.DESKTOP

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any]) -> "ApplicationConfig":
        raw_mode = values.get("QUAPYLAB_MODE", DeploymentMode.DESKTOP)
        try:
            mode = DeploymentMode(raw_mode)
        except ValueError as error:
            choices = ", ".join(item.value for item in DeploymentMode)
            raise ValueError(f"QUAPYLAB_MODE must be one of: {choices}.") from error
        return cls(mode=mode)
