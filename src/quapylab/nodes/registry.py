"""Server-owned node definitions.  No runtime module paths are accepted."""

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class PortDefinition:
    name: str
    data_type: str
    required: bool = True


@dataclass(frozen=True, slots=True)
class NodeDefinition:
    type: str
    version: int
    title: str
    category: str
    inputs: tuple[PortDefinition, ...] = ()
    outputs: tuple[PortDefinition, ...] = ()
    config_schema: dict[str, Any] | None = None

    def as_public_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "version": self.version,
            "title": self.title,
            "category": self.category,
            "inputs": _public_ports(self.inputs),
            "outputs": _public_ports(self.outputs),
            "config_schema": self.config_schema
            or {"type": "object", "additionalProperties": False},
        }


def _public_ports(ports: tuple[PortDefinition, ...]) -> list[dict[str, object]]:
    return [
        {"name": port.name, "data_type": port.data_type, "required": port.required}
        for port in ports
    ]


class NodeRegistry:
    def __init__(self, definitions: tuple[NodeDefinition, ...]) -> None:
        self._definitions = {(item.type, item.version): item for item in definitions}

    def get(self, node_type: str, version: int) -> NodeDefinition | None:
        return self._definitions.get((node_type, version))

    def public_definitions(self) -> list[dict[str, Any]]:
        return [
            item.as_public_dict()
            for item in sorted(self._definitions.values(), key=lambda item: item.type)
        ]


def default_registry() -> NodeRegistry:
    labelled = "labelled_collection"
    return NodeRegistry(
        (
            NodeDefinition(
                "dataset",
                1,
                "Dataset",
                "data",
                outputs=(PortDefinition("dataset", labelled),),
                config_schema={
                    "type": "object",
                    "required": ["dataset_version_id"],
                    "properties": {
                        "dataset_version_id": {"type": "string", "format": "uuid"},
                        "representation": {
                            "type": "string",
                            "enum": ["tabular", "text", "precomputed_matrix"],
                            "default": "tabular",
                        },
                        "label_column": {"type": "string"},
                        "feature_columns": {"type": "array", "items": {"type": "string"}},
                        "text_column": {"type": "string"},
                    },
                    "additionalProperties": False,
                },
            ),
            NodeDefinition(
                "split",
                1,
                "Split",
                "data",
                inputs=(PortDefinition("dataset", labelled),),
                outputs=(PortDefinition("train", labelled),),
                config_schema={
                    "type": "object",
                    "properties": {
                        "train_proportion": {
                            "type": "number",
                            "minimum": 0.1,
                            "maximum": 0.9,
                            "default": 0.7,
                        }
                    },
                    "additionalProperties": False,
                },
            ),
            NodeDefinition(
                "classifier",
                1,
                "Classifier",
                "model",
                inputs=(PortDefinition("train", labelled),),
                outputs=(PortDefinition("classifier", "classifier"),),
                config_schema={
                    "type": "object",
                    "properties": {
                        "max_iter": {"type": "integer", "minimum": 1, "default": 1000},
                        "seed": {"type": "integer"},
                    },
                    "additionalProperties": False,
                },
            ),
            NodeDefinition(
                "acc",
                1,
                "ACC",
                "quantifier",
                inputs=(PortDefinition("train", labelled),),
                outputs=(PortDefinition("quantifier", "fitted_quantifier"),),
            ),
            NodeDefinition(
                "evaluation",
                1,
                "Evaluation",
                "evaluation",
                inputs=(PortDefinition("quantifier", "fitted_quantifier"),),
                outputs=(PortDefinition("report", "evaluation_report"),),
            ),
        )
    )
