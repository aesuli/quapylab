# ruff: noqa: E501
"""Generate standalone, direct-QuaPy Python programs from workflow graphs."""

from __future__ import annotations

import json
from typing import Any


def generate_quapy_script(name: str, graph: dict[str, Any], datasets: list[dict[str, Any]]) -> str:
    """Return a script that depends on QuaPy/scikit-learn, never QuaPy Lab."""
    payload = json.dumps(graph, separators=(",", ":"))
    requirements = json.dumps(datasets, separators=(",", ":"))
    return (
        _TEMPLATE.replace("__WORKFLOW_NAME__", repr(name))
        .replace("__WORKFLOW_JSON__", repr(payload))
        .replace("__DATASETS_JSON__", repr(requirements))
    )


_TEMPLATE = '''#!/usr/bin/env python3
"""Standalone QuaPy implementation exported from a QuaPy Lab workflow."""
from __future__ import annotations

import csv
import inspect
import json
from pathlib import Path

import numpy as np
import quapy as qp
import quapy.method.aggregative as aggregative
from quapy.data.base import LabelledCollection
from quapy.model_selection import GridSearchQ
from quapy.protocol import APP, NPP
from sklearn.ensemble import RandomForestClassifier
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import train_test_split
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import MinMaxScaler, RobustScaler, StandardScaler

WORKFLOW_NAME = __WORKFLOW_NAME__
WORKFLOW = json.loads(__WORKFLOW_JSON__)
DATASETS = json.loads(__DATASETS_JSON__)
SEED = 0


def _dataset(node):
    version_id = node["config"]["dataset_version_id"]
    descriptor = next(item for item in DATASETS if item["version_id"] == version_id)
    source = descriptor.get("source", {})
    if source.get("provider") == "quapy":
        from quapy.data import datasets
        loaders = {
            "reviews": datasets.fetch_reviews,
            "twitter": datasets.fetch_twitter,
            "uci_binary": datasets.fetch_UCIBinaryDataset,
            "uci_multiclass": datasets.fetch_UCIMulticlassDataset,
        }
        data = loaders[source["loader"]](source["dataset"], **source.get("options", {}))
        return data.training + data.test
    path = Path(__file__).resolve().parent / descriptor["expected_path"]
    delimiter = "\\t" if source.get("format") == "tsv" else ","
    with path.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream, delimiter=delimiter))
    config = node["config"]
    label = config.get("label_column") or descriptor.get("label_column")
    if not label:
        raise ValueError(f"Dataset {descriptor['name']!r} needs a label_column.")
    representation = config.get("representation", "tabular")
    if representation == "text":
        instances = np.asarray([row[config["text_column"]] for row in rows], dtype=object)
    else:
        features = config.get("feature_columns") or [key for key in rows[0] if key != label]
        instances = np.asarray(
            [[float(row[key]) if row[key] else np.nan for key in features] for row in rows]
        )
    labels = np.asarray([row[label] for row in rows])
    return LabelledCollection(instances, labels, classes=descriptor.get("classes") or None)


def _split(collection, config):
    indices = np.arange(len(collection))
    train_size = config.get("train_proportion", 0.7)
    train, remainder = train_test_split(
        indices, train_size=train_size, random_state=SEED, stratify=collection.labels
    )
    validation_size = config.get("validation_proportion", 0)
    validation = np.asarray([], dtype=int)
    test = remainder
    if validation_size:
        fraction = validation_size / (1 - train_size)
        validation, test = train_test_split(
            remainder,
            train_size=fraction,
            random_state=SEED,
            stratify=collection.labels[remainder],
        )
    return {
        "train": collection.sampling_from_index(train),
        "validation": collection.sampling_from_index(validation) if len(validation) else None,
        "test": collection.sampling_from_index(test),
    }


def _classifier(kind, config):
    if kind == "logistic_regression":
        return LogisticRegression(
            max_iter=config.get("max_iter", 1000), C=config.get("c", 1.0),
            random_state=config.get("seed", SEED)
        )
    return RandomForestClassifier(
        n_estimators=config.get("n_estimators", 200),
        max_depth=config.get("max_depth"), random_state=config.get("seed", SEED), n_jobs=1
    )


def _preprocess(train, test, config):
    steps = []
    imputation = config.get("imputation", "none")
    scaling = config.get("scaling", "standard")
    if imputation != "none":
        steps.append(("imputer", SimpleImputer(strategy=imputation)))
    scalers = {"standard": StandardScaler(), "minmax": MinMaxScaler(), "robust": RobustScaler()}
    if scaling != "none":
        steps.append(("scaler", scalers[scaling]))
    transformer = Pipeline(steps) if steps else None
    if transformer is None:
        return train, test, None
    return (
        LabelledCollection(transformer.fit_transform(train.instances), train.labels, classes=train.classes_),
        LabelledCollection(transformer.transform(test.instances), test.labels, classes=test.classes_),
        transformer,
    )


def _quantifier(method, classifier, config):
    names = {"acc": "ACC", "pcc": "PCC", "pacc": "PACC", "emq": "EMQ", "hdy": "HDy", "dmy": "DMy"}
    defaults = {
        "acc": {"fit_classifier": True, "n_jobs": 1, "val_split": 5, "solver": "minimize", "method": "inversion", "norm": "clip"},
        "pcc": {"fit_classifier": True, "val_split": None},
        "pacc": {"fit_classifier": True, "n_jobs": 1, "val_split": 5, "solver": "minimize", "method": "inversion", "norm": "clip"},
        "emq": {"fit_classifier": True, "n_jobs": 1, "val_split": None, "exact_train_prev": True, "calib": "none", "on_calib_error": "raise"},
        "hdy": {"fit_classifier": True, "val_split": 5},
        "dmy": {"fit_classifier": True, "n_jobs": 1, "val_split": 5, "nbins": 8, "divergence": "HD", "cdf": False, "search": "optim_minimize"},
    }
    cls = getattr(aggregative, names[method])
    options = {**defaults[method], **config}
    accepted = inspect.signature(cls).parameters
    return cls(classifier, **{key: value for key, value in options.items() if key in accepted})


def _protocol(kind, collection, config):
    options = dict(config)
    options.pop("return_type", None)
    seed = options.pop("seed", SEED)
    if options.get("sample_size") is None:
        options["sample_size"] = len(collection)
    return (APP if kind == "app" else NPP)(collection, random_state=seed, **options)


def _fit(model, collection, method):
    labels = collection.labels
    if method == "hdy":
        indices = {label: index for index, label in enumerate(collection.classes_)}
        labels = np.asarray([indices[label] for label in labels])
    if len(inspect.signature(model.fit).parameters) >= 2:
        model.fit(collection.instances, labels)
    else:
        model.fit(collection)
    return model


def _inputs(node_id, values):
    incoming = [edge for edge in WORKFLOW["edges"] if edge["target"] == node_id]
    return {edge["target_port"]: values[(edge["source"], edge["source_port"])] for edge in incoming}


def _order():
    pending = {node["id"]: node for node in WORKFLOW["nodes"]}
    completed = set()
    while pending:
        ready = next((node for node in pending.values() if all(
            edge["source"] in completed for edge in WORKFLOW["edges"] if edge["target"] == node["id"]
        )), None)
        if ready is None:
            raise ValueError("Workflow graph contains a cycle.")
        yield ready
        completed.add(ready["id"])
        del pending[ready["id"]]


def run():
    values = {}
    results = {}
    for node in _order():
        node_id, kind, config = node["id"], node["type"], node.get("config", {})
        inputs = _inputs(node_id, values)
        if kind == "dataset":
            outputs = {"dataset": _dataset(node)}
        elif kind == "split":
            outputs = _split(inputs["dataset"], config)
        elif kind in {"logistic_regression", "random_forest"}:
            outputs = {"classifier": _classifier(kind, config)}
        elif kind == "preprocessing":
            train, test, transformer = _preprocess(inputs["train"], inputs["test"], config)
            outputs = {"train": train, "test": test, "transformer": transformer}
        elif kind in {"acc", "pcc", "pacc", "emq", "hdy", "dmy"}:
            outputs = {"quantifier": (kind, inputs["classifier"], config)}
        elif kind == "fit_quantifier":
            method, classifier, options = inputs["quantifier"]
            model = _fit(_quantifier(method, classifier, options), inputs["train"], method)
            outputs = {"quantifier": model}
        elif kind in {"app", "npp"}:
            outputs = {"protocol": _protocol(kind, inputs["collection"], config)}
        elif kind == "evaluation":
            actual = inputs["test"].prevalence()
            estimated = inputs["quantifier"].quantify(inputs["test"].instances)
            outputs = {"report": {"true_prevalence": actual.tolist(), "estimated_prevalence": estimated.tolist(), "mae": float(qp.error.ae(actual, estimated))}}
        elif kind == "protocol_evaluation":
            samples = []
            for instances, actual in inputs["protocol"]():
                estimated = inputs["quantifier"].quantify(instances)
                samples.append({"true_prevalence": np.asarray(actual).tolist(), "estimated_prevalence": np.asarray(estimated).tolist(), "ae": float(qp.error.ae(actual, estimated))})
            outputs = {"report": {"samples": samples, "mae": float(np.mean([item["ae"] for item in samples]))}}
        elif kind == "model_selection":
            method, classifier, options = inputs["quantifier"]
            model = _quantifier(method, classifier, options)
            parameter = "classifier__C" if isinstance(classifier, LogisticRegression) else "classifier__n_estimators"
            search = GridSearchQ(model, {parameter: config.get("classifier_values", [0.1, 1.0, 10.0])}, inputs["validation_protocol"], error=config.get("error", "mae"), refit=config.get("refit", True), timeout=config.get("timeout", -1), n_jobs=1, raise_errors=config.get("raise_errors", False), verbose=config.get("verbose", False))
            search.fit(inputs["train"])
            outputs = {"quantifier": search.best_model_, "selection": {"best_score": float(search.best_score_)}}
        elif kind == "report":
            outputs = {}
            results[node_id] = inputs["evaluation"]
        elif kind == "comparison":
            compared = [value for key, value in inputs.items() if key.startswith("evaluation_")]
            outputs = {"comparison": compared, "report": compared}
            results[node_id] = compared
        else:
            raise ValueError(f"Unsupported exported node type: {kind}")
        for port, value in outputs.items():
            values[(node_id, port)] = value
        if kind in {"evaluation", "protocol_evaluation", "model_selection"}:
            results[node_id] = next(iter(outputs.values()))
    return results


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))
'''
