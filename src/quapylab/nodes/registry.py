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
                    PortDefinition("validation", labelled, required=False),
                ),
                config_schema={
                    "type": "object",
                    "properties": {
                        "train_proportion": {
                            "type": "number",
                            "minimum": 0.1,
                            "maximum": 0.9,
                            "default": 0.7,
                        },
                        "validation_proportion": {
                            "type": "number",
                            "minimum": 0,
                            "maximum": 0.4,
                            "default": 0,
                        },
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
                        "kind": {
                            "type": "string",
                            "enum": ["logistic_regression", "random_forest"],
                            "default": "logistic_regression",
                        },
                        "max_iter": {"type": "integer", "minimum": 1, "default": 1000},
                        "c": {"type": "number", "exclusiveMinimum": 0, "default": 1.0},
                        "n_estimators": {"type": "integer", "minimum": 1, "default": 200},
                        "max_depth": {
                            "type": ["integer", "null"],
                            "minimum": 1,
                            "default": None,
                        },
                        "seed": {"type": "integer"},
                    },
                    "additionalProperties": False,
                },
                capabilities={
                    "allowlist": ["logistic_regression", "random_forest"],
                    "predict_proba": True,
                },
            ),
            NodeDefinition(
                "preprocessing",
                1,
                "Tabular preprocessing",
                "preprocessing",
                inputs=(
                    PortDefinition("train", labelled),
                    PortDefinition("test", labelled),
                ),
                outputs=(
                    PortDefinition("train", labelled),
                    PortDefinition("test", labelled),
                    PortDefinition("transformer", "transformer"),
                ),
                config_schema={
                    "type": "object",
                    "properties": {
                        "imputation": {
                            "type": "string",
                            "enum": ["none", "mean", "median", "most_frequent"],
                            "default": "none",
                        },
                        "scaling": {
                            "type": "string",
                            "enum": ["none", "standard", "minmax", "robust"],
                            "default": "standard",
                        },
                    },
                    "additionalProperties": False,
                },
                capabilities={"representation": "dense_tabular", "fit_scope": "train_only"},
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
            NodeDefinition(
                "comparison",
                1,
                "Method comparison",
                "output",
                inputs=(
                    PortDefinition("evaluation_1", "evaluation_report"),
                    PortDefinition("evaluation_2", "evaluation_report"),
                    PortDefinition("evaluation_3", "evaluation_report", required=False),
                    PortDefinition("evaluation_4", "evaluation_report", required=False),
                ),
                outputs=(
                    PortDefinition("comparison", "comparison_report"),
                    PortDefinition("report", "report"),
                ),
                capabilities={"paired_samples": True, "maximum_methods": 4},
            ),
            NodeDefinition(
                "model_selection",
                1,
                "GridSearchQ model selection",
                "optimization",
                inputs=(
                    PortDefinition("train", labelled),
                    PortDefinition("classifier", "classifier_spec"),
                    PortDefinition("validation_protocol", "protocol"),
                ),
                outputs=(
                    PortDefinition("quantifier", "fitted_quantifier"),
                    PortDefinition("selection", "model_selection_result"),
                ),
                config_schema={
                    "type": "object",
                    "properties": {
                        "method": {
                            "type": "string",
                            "enum": ["acc", "pacc"],
                            "default": "acc",
                        },
                        "classifier_values": {
                            "type": "array",
                            "items": {"type": "number"},
                            "default": [0.1, 1.0, 10.0],
                        },
                        "error": {
                            "type": "string",
                            "enum": ["mae", "mrae", "mse", "mkld", "mnkld", "mnae", "mnrae"],
                            "default": "mae",
                        },
                        "refit": {"type": "boolean", "default": True},
                    },
                    "additionalProperties": False,
                },
                capabilities={
                    "implementation": "quapy.model_selection.GridSearchQ",
                    "n_jobs": 1,
                    "validation_role": "required",
                },
            ),
        )
    )
