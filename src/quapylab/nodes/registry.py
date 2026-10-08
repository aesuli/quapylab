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
    capabilities: dict[str, Any] | None = None

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
            "capabilities": self.capabilities or {},
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
                outputs=(
                    PortDefinition("train", labelled),
                    PortDefinition("test", labelled),
                ),
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
                outputs=(PortDefinition("classifier", "classifier_spec"),),
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
                inputs=(
                    PortDefinition("train", labelled),
                    PortDefinition("classifier", "classifier_spec"),
                ),
                outputs=(PortDefinition("quantifier", "fitted_quantifier"),),
                capabilities={"class_cardinality": "binary_or_multiclass"},
            ),
            *(
                NodeDefinition(
                    method,
                    1,
                    title,
                    "quantifier",
                    inputs=(
                        PortDefinition("train", labelled),
                        PortDefinition("classifier", "classifier_spec"),
                    ),
                    outputs=(PortDefinition("quantifier", "fitted_quantifier"),),
                    capabilities={
                        "class_cardinality": cardinality,
                        "requires_probabilistic_classifier": probabilistic,
                        "optional_dependency": None,
                    },
                )
                for method, title, cardinality, probabilistic in (
                    ("pcc", "PCC", "binary_or_multiclass", True),
                    ("pacc", "PACC", "binary_or_multiclass", True),
                    ("emq", "EMQ", "binary_or_multiclass", True),
                    ("hdy", "HDy", "binary", True),
                    ("dmy", "DMy", "binary_or_multiclass", True),
                )
            ),
            NodeDefinition(
                "evaluation",
                1,
                "Evaluation",
                "evaluation",
                inputs=(
                    PortDefinition("quantifier", "fitted_quantifier"),
                    PortDefinition("test", labelled),
                ),
                outputs=(PortDefinition("report", "evaluation_report"),),
            ),
            NodeDefinition(
                "app",
                1,
                "Artificial Prevalence Protocol",
                "protocol",
                inputs=(PortDefinition("collection", labelled),),
                outputs=(PortDefinition("protocol", "protocol"),),
                config_schema={
                    "type": "object",
                    "properties": {
                        "sample_size": {"type": "integer", "minimum": 1, "default": 100},
                        "n_prevalences": {"type": "integer", "minimum": 2, "default": 21},
                        "repeats": {"type": "integer", "minimum": 1, "default": 1},
                        "seed": {"type": "integer"},
                    },
                    "additionalProperties": False,
                },
            ),
            NodeDefinition(
                "npp",
                1,
                "Natural Prevalence Protocol",
                "protocol",
                inputs=(PortDefinition("collection", labelled),),
                outputs=(PortDefinition("protocol", "protocol"),),
                config_schema={
                    "type": "object",
                    "properties": {
                        "sample_size": {"type": "integer", "minimum": 1, "default": 100},
                        "repeats": {"type": "integer", "minimum": 1, "default": 100},
                        "seed": {"type": "integer"},
                    },
                    "additionalProperties": False,
                },
            ),
            NodeDefinition(
                "protocol_evaluation",
                1,
                "Protocol Evaluation",
                "evaluation",
                inputs=(
                    PortDefinition("quantifier", "fitted_quantifier"),
                    PortDefinition("protocol", "protocol"),
                ),
                outputs=(PortDefinition("report", "evaluation_report"),),
            ),
            NodeDefinition(
                "report",
                1,
                "Report",
                "output",
                inputs=(PortDefinition("evaluation", "evaluation_report"),),
            ),
        )
    )
