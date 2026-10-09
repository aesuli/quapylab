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


def _val_split(default: int | None) -> dict[str, Any]:
    return {
        "title": "Classifier validation split",
        "description": (
            "Integer cross-validation folds, a fraction in (0,1), or empty for no split."
        ),
        "type": ["number", "null"],
        "exclusiveMinimum": 0,
        "default": default,
    }


def _quantifier_schema(method: str) -> dict[str, Any]:
    properties: dict[str, Any] = {
        "fit_classifier": {
            "type": "boolean",
            "default": True,
            "readOnly": True,
            "description": (
                "Always enabled because classifier nodes provide an unfitted specification."
            ),
        }
    }
    if method in {"acc", "pacc", "emq", "dmy"}:
        properties["n_jobs"] = {
            "type": "integer",
            "default": 1,
            "readOnly": True,
            "description": "Fixed by the local resource policy to prevent nested parallelism.",
        }
    if method in {"pcc", "emq"}:
        properties["val_split"] = _val_split(None)
    elif method in {"acc", "pacc", "hdy", "dmy"}:
        properties["val_split"] = _val_split(5)
    if method in {"acc", "pacc"}:
        properties.update(
            {
                "solver": {
                    "type": "string",
                    "enum": ["minimize", "exact-raise", "exact-cc"],
                    "default": "minimize",
                },
                "method": {
                    "title": "Adjustment method",
                    "type": "string",
                    "enum": ["inversion", "invariant-ratio"],
                    "default": "inversion",
                },
                "norm": {
                    "title": "Prevalence normalization",
                    "type": "string",
                    "enum": ["clip", "mapsimplex", "condsoftmax"],
                    "default": "clip",
                },
            }
        )
    elif method == "emq":
        properties.update(
            {
                "exact_train_prev": {"type": "boolean", "default": True},
                "calib": {
                    "title": "Posterior calibration",
                    "type": "string",
                    "enum": ["none", "nbvs", "bcts", "ts", "vs"],
                    "default": "none",
                },
                "on_calib_error": {
                    "title": "Calibration failure",
                    "type": "string",
                    "enum": ["raise", "backup"],
                    "default": "raise",
                    "x-show-when": {"calib": ["nbvs", "bcts", "ts", "vs"]},
                },
            }
        )
    elif method == "dmy":
        properties.update(
            {
                "nbins": {"type": "integer", "minimum": 2, "default": 8},
                "divergence": {
                    "type": "string",
                    "enum": ["HD", "topsoe"],
                    "default": "HD",
                },
                "cdf": {"type": "boolean", "default": False},
                "search": {
                    "type": "string",
                    "enum": ["optim_minimize", "linear_search", "ternary_search"],
                    "default": "optim_minimize",
                },
            }
        )
    return {"type": "object", "properties": properties, "additionalProperties": False}


def _classifier_algorithm_schema(kind: str) -> dict[str, Any]:
    if kind == "logistic_regression":
        properties = {
            "max_iter": {"type": "integer", "minimum": 1, "default": 1000},
            "c": {"type": "number", "exclusiveMinimum": 0, "default": 1.0},
            "seed": {"type": "integer"},
        }
    else:
        properties = {
            "n_estimators": {"type": "integer", "minimum": 1, "default": 200},
            "max_depth": {
                "type": ["integer", "null"],
                "minimum": 1,
                "default": None,
            },
            "seed": {"type": "integer"},
        }
    return {"type": "object", "properties": properties, "additionalProperties": False}


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
            *(
                NodeDefinition(
                    kind,
                    1,
                    title,
                    "classifier",
                    outputs=(PortDefinition("classifier", "classifier_spec"),),
                    config_schema=_classifier_algorithm_schema(kind),
                    capabilities={"algorithm": kind, "predict_proba": True},
                )
                for kind, title in (
                    ("logistic_regression", "Logistic regression"),
                    ("random_forest", "Random forest"),
                )
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
                        "sample_size": {
                            "type": ["integer", "null"],
                            "minimum": 1,
                            "default": None,
                            "description": "Empty uses the entire collection size.",
                        },
                        "n_prevalences": {"type": "integer", "minimum": 2, "default": 21},
                        "repeats": {"type": "integer", "minimum": 1, "default": 10},
                        "smooth_limits_epsilon": {
                            "type": "number",
                            "minimum": 0,
                            "maximum": 0.49,
                            "default": 0,
                        },
                        "sanity_check": {
                            "type": "integer",
                            "minimum": 1,
                            "default": 10000,
                        },
                        "return_type": {
                            "type": "string",
                            "enum": ["sample_prev"],
                            "default": "sample_prev",
                            "readOnly": True,
                            "description": (
                                "Fixed to the paired sample representation used by evaluation."
                            ),
                        },
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
                        "sample_size": {
                            "type": ["integer", "null"],
                            "minimum": 1,
                            "default": None,
                            "description": "Empty uses the entire collection size.",
                        },
                        "repeats": {"type": "integer", "minimum": 1, "default": 100},
                        "return_type": {
                            "type": "string",
                            "enum": ["sample_prev"],
                            "default": "sample_prev",
                            "readOnly": True,
                            "description": (
                                "Fixed to the paired sample representation used by evaluation."
                            ),
                        },
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
                    PortDefinition("quantifier", "quantifier_spec"),
                    PortDefinition("validation_protocol", "protocol"),
                ),
                outputs=(
                    PortDefinition("quantifier", "fitted_quantifier"),
                    PortDefinition("selection", "model_selection_result"),
                ),
                config_schema={
                    "type": "object",
                    "properties": {
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
                        "timeout": {
                            "type": "integer",
                            "minimum": -1,
                            "default": -1,
                            "description": "Seconds per candidate; -1 disables the timeout.",
                        },
                        "raise_errors": {"type": "boolean", "default": False},
                        "verbose": {"type": "boolean", "default": False},
                        "n_jobs": {
                            "type": "integer",
                            "default": 1,
                            "readOnly": True,
                            "description": "Fixed to one by the local resource policy.",
                        },
                    },
                    "additionalProperties": False,
                },
                capabilities={
                    "implementation": "quapy.model_selection.GridSearchQ",
                    "n_jobs": 1,
                    "validation_role": "required",
                },
            ),
            *(
                NodeDefinition(
                    method,
                    1,
                    title,
                    "quantifier",
                    inputs=(PortDefinition("classifier", "classifier_spec"),),
                    outputs=(PortDefinition("quantifier", "quantifier_spec"),),
                    config_schema=_quantifier_schema(method),
                    capabilities={
                        "unfitted": True,
                        "class_cardinality": cardinality,
                        "requires_probabilistic_classifier": probabilistic,
                    },
                )
                for method, title, cardinality, probabilistic in (
                    ("acc", "ACC", "binary_or_multiclass", False),
                    ("pcc", "PCC", "binary_or_multiclass", True),
                    ("pacc", "PACC", "binary_or_multiclass", True),
                    ("emq", "EMQ", "binary_or_multiclass", True),
                    ("hdy", "HDy", "binary", True),
                    ("dmy", "DMy", "binary_or_multiclass", True),
                )
            ),
            NodeDefinition(
                "fit_quantifier",
                1,
                "Fit quantifier",
                "quantifier",
                inputs=(
                    PortDefinition("train", labelled),
                    PortDefinition("quantifier", "quantifier_spec"),
                ),
                outputs=(PortDefinition("quantifier", "fitted_quantifier"),),
            ),
        )
    )
