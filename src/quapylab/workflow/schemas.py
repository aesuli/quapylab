"""Pydantic contracts for persisted and API-submitted workflow graphs."""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class WorkflowPosition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    x: float
    y: float


class WorkflowNode(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    type: str = Field(min_length=1, max_length=128)
    version: int = Field(ge=1)
    config: dict[str, Any] = Field(default_factory=dict)
    position: WorkflowPosition | None = None


class WorkflowEdge(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    source: str = Field(min_length=1, max_length=128)
    source_port: str = Field(min_length=1, max_length=128)
    target: str = Field(min_length=1, max_length=128)
    target_port: str = Field(min_length=1, max_length=128)


class WorkflowSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(ge=1)
    nodes: list[WorkflowNode] = Field(default_factory=list)
    edges: list[WorkflowEdge] = Field(default_factory=list)
