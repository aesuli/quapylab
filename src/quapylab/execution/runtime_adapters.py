"""Trusted, server-owned runtime adapters for the initial QuaPy workflow nodes.

The registry identifies adapters by the fixed ``(type, version)`` pair.  Workflow
documents never influence imports, file paths, or executable code.
"""

from __future__ import annotations

import csv
import html
import io
import json
import math
import pickle
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from inspect import signature
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from quapylab.persistence.models import Artifact, Dataset, DatasetVersion, NodeRun, Project, Run
from quapylab.services.artifacts import ArtifactService

Adapter = Callable[[Session, Run, NodeRun], None]

_MATERIALIZATION_MEDIA_TYPE = "application/vnd.quapylab.quapy-materialization+pickle"
_QUANTIFIER_MEDIA_TYPE = "application/vnd.quapylab.quapy-quantifier+pickle"
_SPLIT_MEDIA_TYPE = "application/vnd.quapylab.dataset-split+json"
_PROTOCOL_MEDIA_TYPE = "application/vnd.quapylab.protocol+json"


class RuntimeNodeExecutor:
    """Execute the small allowlisted runtime surface owned by QuaPy Lab."""

    def __init__(self, artifacts: ArtifactService) -> None:
        self._artifacts = artifacts
        self._adapters: dict[tuple[str, int], Adapter] = {
            ("dataset", 1): self._dataset,
            ("split", 1): self._split,
            ("classifier", 1): self._classifier,
            ("acc", 1): self._acc,
            ("pcc", 1): self._pcc,
            ("pacc", 1): self._pacc,
            ("emq", 1): self._emq,
            ("hdy", 1): self._hdy,
            ("dmy", 1): self._dmy,
            ("evaluation", 1): self._evaluation,
            ("app", 1): self._app,
            ("npp", 1): self._npp,
            ("protocol_evaluation", 1): self._protocol_evaluation,
            ("report", 1): self._report,
        }

    def __call__(self, session: Session, run: Run, node: NodeRun) -> None:
        adapter = self._adapters.get((node.node_type, node.node_version))
        if adapter is None:
            raise RuntimeError(
                f"No runtime adapter is registered for {node.node_type!r} v{node.node_version}."
            )
        adapter(session, run, node)

    def _dataset(self, session: Session, run: Run, node: NodeRun) -> None:
        config = _dataset_config(_config(node))
        version = session.get(DatasetVersion, config.version_id)
        if version is None:
            raise ValueError("Dataset configuration references an unknown dataset version.")
        dataset = session.get(Dataset, version.dataset_id)
        if dataset is None or dataset.project_id != run.project_id:
            raise ValueError("Dataset configuration references a dataset outside this project.")
        artifact = _dataset_materialization(
            session, run, node, version, config, self._artifacts, self._store_materialization
        )
        node.output_artifact_ids = [artifact.id]
        _log(node, "dataset.loaded", "Loaded immutable trusted QuaPy materialization.")

    def _split(self, session: Session, run: Run, node: NodeRun) -> None:
        data = self._collection_input(session, run, node, "dataset")
        source = _input_artifact(session, run, node, "dataset")
        version_id = _source_dataset_version_id(session, run, node)
        # Built-in QuaPy datasets are already delivered as deterministic train/test splits.
        if hasattr(data, "training") and hasattr(data, "test"):
            train = self._store_materialization(session, run, node, data.training)
            test = self._store_materialization(session, run, node, data.test)
        else:
            train_proportion = _config(node).get("train_proportion", 0.7)
            if not isinstance(train_proportion, (int, float)) or not 0 < train_proportion < 1:
                raise ValueError("Split train_proportion must be strictly between zero and one.")
            train_indices, test_indices = _stratified_indices(
                data, float(train_proportion), run.seed
            )
            common = {
                "schema_version": 1,
                "source_dataset_version_id": version_id,
                "source_artifact_id": source.id,
                "source_artifact_sha256": source.sha256,
                "seed": run.seed,
                "strategy": "stratified",
                "train_proportion": float(train_proportion),
                "train_indices": train_indices,
                "test_indices": test_indices,
                "class_distribution": {
                    "train": _class_distribution(data, train_indices),
                    "test": _class_distribution(data, test_indices),
                },
            }
            train = self._store_split_manifest(session, run, node, {**common, "partition": "train"})
            test = self._store_split_manifest(session, run, node, {**common, "partition": "test"})
        node.output_artifact_ids = [train.id, test.id]
        _log(node, "split.completed", "Recorded the deterministic training and evaluation split.")

    def _classifier(self, session: Session, run: Run, node: NodeRun) -> None:
        max_iter = _config(node).get("max_iter", 1000)
        seed = _config(node).get("seed", run.seed)
        if not isinstance(max_iter, int) or max_iter < 1 or not isinstance(seed, int):
            raise ValueError("Classifier max_iter and seed must be integers.")
        content = json.dumps(
            {"kind": "logistic_regression", "max_iter": max_iter, "seed": seed},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        artifact = self._artifacts.create(
            session,
            group_id=_run_group_id(session, run),
            producing_run_id=run.id,
            producing_node_run_id=node.id,
            kind="classifier-spec",
            media_type="application/vnd.quapylab.classifier-spec+json",
            content=content,
            serialization={"format": "json", "schema_version": 1},
        )
        node.output_artifact_ids = [artifact.id]
        _log(node, "classifier.resolved", "Resolved the configured logistic-regression classifier.")

    def _acc(self, session: Session, run: Run, node: NodeRun) -> None:
        self._fit_quantifier(session, run, node, "ACC")

    def _pcc(self, session: Session, run: Run, node: NodeRun) -> None:
        self._fit_quantifier(session, run, node, "PCC")

    def _pacc(self, session: Session, run: Run, node: NodeRun) -> None:
        self._fit_quantifier(session, run, node, "PACC")

    def _emq(self, session: Session, run: Run, node: NodeRun) -> None:
        self._fit_quantifier(session, run, node, "EMQ")

    def _hdy(self, session: Session, run: Run, node: NodeRun) -> None:
        self._fit_quantifier(session, run, node, "HDy")

    def _dmy(self, session: Session, run: Run, node: NodeRun) -> None:
        self._fit_quantifier(session, run, node, "DMy")

    def _fit_quantifier(self, session: Session, run: Run, node: NodeRun, method_name: str) -> None:
        training = self._collection_input(session, run, node, "train")
        classifier_spec = self._classifier_spec_input(session, run, node)
        classifier = _classifier_from_spec(classifier_spec)
        try:
            import quapy.method.aggregative as aggregative  # type: ignore[import-untyped]
        except ImportError as error:  # pragma: no cover - deployment dependency failure
            raise RuntimeError("QuaPy is required to execute quantifier nodes.") from error
        quantifier_type = getattr(aggregative, method_name, None)
        if quantifier_type is None:
            raise RuntimeError(f"Installed QuaPy does not provide {method_name}.")
        try:
            constructor = signature(quantifier_type).parameters
            options = {"n_jobs": 1} if "n_jobs" in constructor else {}
            quantifier = quantifier_type(classifier, **options)
        except TypeError:  # pragma: no cover - accommodates a QuaPy-compatible adapter surface
            quantifier = quantifier_type()
            quantifier.classifier = classifier
        if len(signature(quantifier.fit).parameters) >= 2:
            quantifier.fit(training.instances, training.labels)
        else:  # Supports QuaPy-compatible adapters with a collection-oriented fit contract.
            quantifier.fit(training)
        content = pickle.dumps(quantifier, protocol=pickle.HIGHEST_PROTOCOL)
        artifact = self._artifacts.create(
            session,
            group_id=_run_group_id(session, run),
            producing_run_id=run.id,
            producing_node_run_id=node.id,
            kind="quapy-quantifier",
            media_type=_QUANTIFIER_MEDIA_TYPE,
            content=content,
            serialization={"format": "pickle", "trusted_producer": "workflow-runtime"},
        )
        node.output_artifact_ids = [artifact.id]
        _log(
            node,
            f"{method_name.lower()}.fitted",
            f"Fitted {method_name} quantifier on the training split.",
        )

    def _evaluation(self, session: Session, run: Run, node: NodeRun) -> None:
        test = self._collection_input(session, run, node, "test")
        quantifier = self._quantifier_input(session, run, node)
        estimated = [float(value) for value in quantifier.quantify(test.instances)]
        actual = [float(value) for value in test.prevalence()]
        if len(estimated) != len(actual):
            raise RuntimeError("Quantifier output does not match the dataset class cardinality.")
        _validate_prevalence(estimated, "estimated")
        _validate_prevalence(actual, "true")
        metrics = _evaluation_metrics(actual, estimated, len(test.instances))
        result = {
            "schema_version": 1,
            "quantifier": _input_source_node(session, run, node, "quantifier").node_type,
            "metric": "mae",
            "value": metrics["ae"],
            "metrics": metrics,
            "estimated_prevalence": estimated,
            "true_prevalence": actual,
            "classes": [str(value) for value in test.classes_],
        }
        content = json.dumps(result, sort_keys=True, separators=(",", ":")).encode("utf-8")
        artifact = self._artifacts.create(
            session,
            group_id=_run_group_id(session, run),
            producing_run_id=run.id,
            producing_node_run_id=node.id,
            kind="evaluation-result",
            media_type="application/vnd.quapylab.evaluation-result+json",
            content=content,
            serialization={"format": "json", "schema_version": 1},
        )
        node.output_artifact_ids = [artifact.id]
        _log(node, "evaluation.completed", "Computed normalized ACC evaluation result.")

    def _app(self, session: Session, run: Run, node: NodeRun) -> None:
        self._store_protocol(session, run, node, "app")

    def _npp(self, session: Session, run: Run, node: NodeRun) -> None:
        self._store_protocol(session, run, node, "npp")

    def _store_protocol(self, session: Session, run: Run, node: NodeRun, kind: str) -> None:
        source = _input_artifact(session, run, node, "collection")
        config = _protocol_config(kind, _config(node), run.seed)
        specification = {
            "schema_version": 1,
            "kind": kind,
            "source_artifact_id": source.id,
            "source_artifact_sha256": source.sha256,
            "config": config,
        }
        artifact = self._artifacts.create(
            session,
            group_id=_run_group_id(session, run),
            producing_run_id=run.id,
            producing_node_run_id=node.id,
            kind="quantification-protocol",
            media_type=_PROTOCOL_MEDIA_TYPE,
            content=json.dumps(specification, sort_keys=True, separators=(",", ":")).encode(),
            serialization={"format": "json", "schema_version": 1},
        )
        node.output_artifact_ids = [artifact.id]
        _log(node, "protocol.resolved", f"Resolved the {kind.upper()} sampling protocol.")

    def _protocol_evaluation(self, session: Session, run: Run, node: NodeRun) -> None:
        quantifier = self._quantifier_input(session, run, node)
        artifact = _input_artifact(session, run, node, "protocol")
        protocol, classes = _load_protocol(session, run, artifact, self._artifacts)
        samples: list[dict[str, Any]] = []
        for index, (instances, true_values) in enumerate(protocol()):
            estimated = [float(value) for value in quantifier.quantify(instances)]
            actual = [float(value) for value in true_values]
            if len(estimated) != len(actual):
                raise RuntimeError("Quantifier output does not match protocol prevalence.")
            _validate_prevalence(estimated, "estimated")
            _validate_prevalence(actual, "true")
            samples.append(
                {
                    "sample_id": f"sample-{index:06d}",
                    "true_prevalence": actual,
                    "estimated_prevalence": estimated,
                    "metrics": _evaluation_metrics(actual, estimated, len(instances)),
                }
            )
        if not samples:
            raise RuntimeError("Protocol did not generate any evaluation samples.")
        aggregate_names = {
            "ae": "mae",
            "rae": "mrae",
            "se": "mse",
            "kld": "mkld",
            "nkld": "mnkld",
            "nae": "mnae",
            "nrae": "mnrae",
        }
        result = {
            "schema_version": 2,
            "protocol": json.loads(self._artifacts.read_for_worker(artifact))["kind"],
            "quantifier": _input_source_node(session, run, node, "quantifier").node_type,
            "classes": classes,
            "metrics": {
                aggregate: sum(item["metrics"][base] for item in samples) / len(samples)
                for base, aggregate in aggregate_names.items()
            },
            "samples": samples,
        }
        output = self._artifacts.create(
            session,
            group_id=_run_group_id(session, run),
            producing_run_id=run.id,
            producing_node_run_id=node.id,
            kind="evaluation-result",
            media_type="application/vnd.quapylab.evaluation-result+json",
            content=json.dumps(result, sort_keys=True, separators=(",", ":")).encode(),
            serialization={"format": "json", "schema_version": 2},
        )
        node.output_artifact_ids = [output.id]
        _log(node, "evaluation.completed", "Evaluated all normalized protocol samples.")

    def _report(self, session: Session, run: Run, node: NodeRun) -> None:
        artifact = _input_artifact(session, run, node, "evaluation")
        if (
            artifact.kind != "evaluation-result"
            or artifact.media_type != "application/vnd.quapylab.evaluation-result+json"
            or artifact.serialization
            not in (
                {"format": "json", "schema_version": 1},
                {"format": "json", "schema_version": 2},
            )
        ):
            raise RuntimeError("Report input is not a trusted evaluation result.")
        evaluation = json.loads(self._artifacts.read_for_worker(artifact))
        if not isinstance(evaluation, dict):
            raise RuntimeError("Evaluation result is malformed.")
        content = _report_html(
            evaluation,
            run=run,
            nodes=list(session.scalars(select(NodeRun).where(NodeRun.run_id == run.id))),
        ).encode("utf-8")
        report = self._artifacts.create(
            session,
            group_id=_run_group_id(session, run),
            producing_run_id=run.id,
            producing_node_run_id=node.id,
            kind="evaluation-report",
            media_type="text/html",
            content=content,
            serialization={"format": "html", "schema_version": 1},
        )
        node.output_artifact_ids = [report.id]
        _log(
            node,
            "report.completed",
            "Rendered the evaluation result as a self-contained HTML report.",
        )

    def _collection_input(self, session: Session, run: Run, node: NodeRun, port: str) -> Any:
        artifact = _input_artifact(session, run, node, port)
        if artifact.kind == "dataset-split" and artifact.media_type == _SPLIT_MEDIA_TYPE:
            return _load_split_partition(session, run, artifact, self._artifacts)
        if (
            artifact.kind != "quapy-materialization"
            or artifact.media_type != _MATERIALIZATION_MEDIA_TYPE
            or artifact.serialization
            not in (
                {"format": "pickle", "trusted_producer": "quapy-loader"},
                {"format": "pickle", "trusted_producer": "workflow-runtime"},
            )
        ):
            raise RuntimeError(f"Input {port!r} is not a trusted labelled collection.")
        return _load_pickle(self._artifacts, artifact)

    def _store_split_manifest(
        self, session: Session, run: Run, node: NodeRun, manifest: dict[str, Any]
    ) -> Artifact:
        return self._artifacts.create(
            session,
            group_id=_run_group_id(session, run),
            producing_run_id=run.id,
            producing_node_run_id=node.id,
            kind="dataset-split",
            media_type=_SPLIT_MEDIA_TYPE,
            content=json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8"),
            serialization={"format": "json", "schema_version": 1},
        )

    def _classifier_spec_input(self, session: Session, run: Run, node: NodeRun) -> dict[str, Any]:
        artifact = _input_artifact(session, run, node, "classifier")
        if (
            artifact.kind != "classifier-spec"
            or artifact.media_type != "application/vnd.quapylab.classifier-spec+json"
            or artifact.serialization != {"format": "json", "schema_version": 1}
        ):
            raise RuntimeError("Classifier input is not a trusted classifier specification.")
        result = json.loads(self._artifacts.read_for_worker(artifact))
        if not isinstance(result, dict):
            raise RuntimeError("Classifier specification is malformed.")
        return result

    def _quantifier_input(self, session: Session, run: Run, node: NodeRun) -> Any:
        artifact = _input_artifact(session, run, node, "quantifier")
        if (
            artifact.kind != "quapy-quantifier"
            or artifact.media_type != _QUANTIFIER_MEDIA_TYPE
            or artifact.serialization
            != {"format": "pickle", "trusted_producer": "workflow-runtime"}
        ):
            raise RuntimeError("Quantifier input is not a trusted QuaPy runtime artifact.")
        return _load_pickle(self._artifacts, artifact)

    def _store_materialization(
        self, session: Session, run: Run, node: NodeRun, data: Any
    ) -> Artifact:
        return self._artifacts.create(
            session,
            group_id=_run_group_id(session, run),
            producing_run_id=run.id,
            producing_node_run_id=node.id,
            kind="quapy-materialization",
            media_type=_MATERIALIZATION_MEDIA_TYPE,
            content=pickle.dumps(data, protocol=pickle.HIGHEST_PROTOCOL),
            serialization={"format": "pickle", "trusted_producer": "workflow-runtime"},
        )


@dataclass(frozen=True, slots=True)
class DatasetConfig:
    version_id: str
    representation: str
    label_column: str | None
    feature_columns: tuple[str, ...] | None
    text_column: str | None


def _dataset_config(config: dict[str, Any]) -> DatasetConfig:
    allowed = {
        "dataset_version_id",
        "representation",
        "label_column",
        "feature_columns",
        "text_column",
    }
    if set(config) - allowed:
        raise ValueError("Dataset node config contains unsupported settings.")
    version_id = config.get("dataset_version_id")
    representation = config.get("representation", "tabular")
    label_column = config.get("label_column")
    feature_columns = config.get("feature_columns")
    text_column = config.get("text_column")
    if not isinstance(version_id, str) or not version_id.strip():
        raise ValueError("Dataset node config must contain a non-empty dataset_version_id.")
    if representation not in {"tabular", "text", "precomputed_matrix"}:
        raise ValueError("Dataset node representation is unsupported.")
    if label_column is not None and (not isinstance(label_column, str) or not label_column.strip()):
        raise ValueError("Dataset node label_column must be a non-empty string when supplied.")
    if text_column is not None and (not isinstance(text_column, str) or not text_column.strip()):
        raise ValueError("Dataset node text_column must be a non-empty string when supplied.")
    if feature_columns is not None and (
        not isinstance(feature_columns, list)
        or not all(isinstance(column, str) and column.strip() for column in feature_columns)
        or len(set(feature_columns)) != len(feature_columns)
    ):
        raise ValueError("Dataset node feature_columns must be unique non-empty column names.")
    return DatasetConfig(
        version_id=version_id,
        representation=representation,
        label_column=label_column,
        feature_columns=tuple(feature_columns) if feature_columns is not None else None,
        text_column=text_column,
    )


def _trusted_materialization(session: Session, run: Run, version: DatasetVersion) -> Artifact:
    artifact_id = version.source.get("artifact_id")
    if not isinstance(artifact_id, str):
        raise ValueError("Dataset version has no QuaPy materialization artifact.")
    artifact = session.get(Artifact, artifact_id)
    if artifact is None or artifact.group_id != _run_group_id(session, run):
        raise ValueError("Dataset version does not reference a trusted QuaPy materialization.")
    if artifact.sha256 not in version.artifact_hashes:
        raise ValueError("Dataset version materialization does not match its recorded hash.")
    is_quapy_materialization = (
        artifact.kind == "quapy-materialization"
        and artifact.media_type == _MATERIALIZATION_MEDIA_TYPE
        and artifact.serialization == {"format": "pickle", "trusted_producer": "quapy-loader"}
    )
    is_tabular_source = (
        artifact.kind == "dataset-source"
        and artifact.media_type in {"text/csv", "text/tab-separated-values"}
        and artifact.serialization.get("format") in {"csv", "tsv"}
    )
    if not (is_quapy_materialization or is_tabular_source):
        raise ValueError("Dataset version does not reference a trusted runtime source.")
    return artifact


def _dataset_materialization(
    session: Session,
    run: Run,
    node: NodeRun,
    version: DatasetVersion,
    config: DatasetConfig,
    artifacts: ArtifactService,
    store_materialization: Callable[[Session, Run, NodeRun, Any], Artifact],
) -> Artifact:
    source = _trusted_materialization(session, run, version)
    if source.kind == "quapy-materialization":
        _load_pickle(artifacts, source)
        return source
    if source.kind != "dataset-source":
        raise ValueError("Dataset version has an unsupported runtime source artifact.")
    data = _csv_labelled_collection(artifacts.read_for_worker(source), version, config)
    return store_materialization(session, run, node, data)


def _dataset_output_artifact(session: Session, run: Run, node: NodeRun) -> Artifact:
    if len(node.output_artifact_ids) != 1:
        raise RuntimeError("Dataset node did not produce exactly one materialization artifact.")
    artifact = session.get(Artifact, node.output_artifact_ids[0])
    if (
        artifact is None
        or artifact.group_id != _run_group_id(session, run)
        or artifact.kind != "quapy-materialization"
        or artifact.media_type != _MATERIALIZATION_MEDIA_TYPE
        or artifact.serialization
        not in (
            {"format": "pickle", "trusted_producer": "quapy-loader"},
            {"format": "pickle", "trusted_producer": "workflow-runtime"},
        )
    ):
        raise RuntimeError("Dataset node output is not a trusted materialization.")
    return artifact


def _csv_labelled_collection(content: bytes, version: DatasetVersion, config: DatasetConfig) -> Any:
    label_column = config.label_column or version.ingestion_report.get("label_column")
    if not isinstance(label_column, str) or not label_column:
        raise ValueError("A labelled CSV/TSV dataset requires a configured label column.")
    filename = version.source.get("filename")
    delimiter = "\t" if isinstance(filename, str) and filename.lower().endswith(".tsv") else ","
    try:
        rows = list(csv.DictReader(io.StringIO(content.decode("utf-8-sig")), delimiter=delimiter))
    except UnicodeDecodeError as error:
        raise ValueError("Dataset source is not valid UTF-8.") from error
    columns = [item["name"] for item in version.schema.get("columns", [])]
    if label_column not in columns:
        raise ValueError("Dataset node label_column does not exist in the selected dataset.")
    labels = [row.get(label_column, "") for row in rows]
    if not rows or any(not label for label in labels):
        raise ValueError("Dataset labels must be present for every row.")
    try:
        import numpy as np
        from quapy.data.base import LabelledCollection  # type: ignore[import-untyped]
    except ImportError as error:  # pragma: no cover - supplied by the QuaPy installation
        raise RuntimeError("QuaPy and NumPy are required to materialize a CSV dataset.") from error
    if config.representation == "text":
        text_column = config.text_column
        if text_column not in columns or text_column == label_column:
            raise ValueError(
                "Text datasets require a non-label text_column from the selected dataset."
            )
        return LabelledCollection(
            np.asarray([row[text_column] for row in rows]), np.asarray(labels), classes=None
        )
    if config.representation == "precomputed_matrix":
        raise ValueError("CSV/TSV sources cannot be used as precomputed matrices.")
    features = list(
        config.feature_columns or (column for column in columns if column != label_column)
    )
    if (
        not features
        or label_column in features
        or any(feature not in columns for feature in features)
    ):
        raise ValueError("Tabular datasets require selected non-label feature columns.")
    try:
        matrix = [[float(row[feature]) for feature in features] for row in rows]
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError(
            "Tabular execution supports complete numeric feature columns only."
        ) from error
    return LabelledCollection(
        np.asarray(matrix), np.asarray(labels), classes=version.classes or None
    )


def _load_pickle(artifacts: ArtifactService, artifact: Artifact) -> Any:
    # read_for_worker verifies SHA-256 immediately before the only deserialization step.
    return pickle.loads(artifacts.read_for_worker(artifact))


def _load_split_partition(
    session: Session, run: Run, artifact: Artifact, artifacts: ArtifactService
) -> Any:
    if artifact.serialization != {"format": "json", "schema_version": 1}:
        raise RuntimeError("Split input is not a supported immutable split manifest.")
    manifest = json.loads(artifacts.read_for_worker(artifact))
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise RuntimeError("Split manifest is malformed.")
    source_id = manifest.get("source_artifact_id")
    partition = manifest.get("partition")
    indices = manifest.get(f"{partition}_indices") if partition in {"train", "test"} else None
    source = session.get(Artifact, source_id) if isinstance(source_id, str) else None
    if (
        source is None
        or source.group_id != _run_group_id(session, run)
        or source.sha256 != manifest.get("source_artifact_sha256")
        or not isinstance(indices, list)
        or not all(isinstance(index, int) and index >= 0 for index in indices)
    ):
        raise RuntimeError("Split manifest does not reference a valid immutable source.")
    data = _load_pickle(artifacts, source)
    if not hasattr(data, "sampling_from_index"):
        raise RuntimeError("Split source cannot reconstruct indexed labelled collections.")
    return data.sampling_from_index(indices)


def _protocol_config(kind: str, config: dict[str, Any], run_seed: int) -> dict[str, int]:
    allowed = {"sample_size", "repeats", "seed"}
    if kind == "app":
        allowed.add("n_prevalences")
    if set(config) - allowed:
        raise ValueError(f"{kind.upper()} node config contains unsupported settings.")
    defaults = {"sample_size": 100, "repeats": 1 if kind == "app" else 100, "seed": run_seed}
    if kind == "app":
        defaults["n_prevalences"] = 21
    resolved = {**defaults, **config}
    if any(not isinstance(value, int) for value in resolved.values()):
        raise ValueError("Protocol settings must be integers.")
    if resolved["sample_size"] < 1 or resolved["repeats"] < 1:
        raise ValueError("Protocol sample_size and repeats must be positive.")
    if kind == "app" and resolved["n_prevalences"] < 2:
        raise ValueError("APP n_prevalences must be at least two.")
    return resolved


def _load_protocol(
    session: Session, run: Run, artifact: Artifact, artifacts: ArtifactService
) -> tuple[Any, list[str]]:
    if (
        artifact.kind != "quantification-protocol"
        or artifact.media_type != _PROTOCOL_MEDIA_TYPE
        or artifact.serialization != {"format": "json", "schema_version": 1}
    ):
        raise RuntimeError("Protocol input is not a trusted protocol specification.")
    specification = json.loads(artifacts.read_for_worker(artifact))
    source_id = specification.get("source_artifact_id")
    source = session.get(Artifact, source_id) if isinstance(source_id, str) else None
    if (
        source is None
        or source.group_id != _run_group_id(session, run)
        or source.sha256 != specification.get("source_artifact_sha256")
    ):
        raise RuntimeError("Protocol does not reference a valid immutable collection.")
    if source.kind == "dataset-split" and source.media_type == _SPLIT_MEDIA_TYPE:
        collection = _load_split_partition(session, run, source, artifacts)
    elif (
        source.kind == "quapy-materialization" and source.media_type == _MATERIALIZATION_MEDIA_TYPE
    ):
        collection = _load_pickle(artifacts, source)
    else:
        raise RuntimeError("Protocol source is not a trusted labelled collection.")
    kind, config = specification.get("kind"), specification.get("config")
    if kind not in {"app", "npp"} or not isinstance(config, dict):
        raise RuntimeError("Protocol specification is malformed.")
    try:
        from quapy.protocol import APP, NPP  # type: ignore[import-untyped]
    except ImportError as error:  # pragma: no cover - deployment dependency failure
        raise RuntimeError("QuaPy is required to execute protocols.") from error
    protocol_type = APP if kind == "app" else NPP
    protocol = protocol_type(
        collection,
        random_state=config["seed"],
        **{key: value for key, value in config.items() if key != "seed"},
    )
    return protocol, [str(value) for value in collection.classes_]


def _source_dataset_version_id(session: Session, run: Run, split_node: NodeRun) -> str:
    bindings = split_node.resolved_config.get("__inputs", {})
    binding = bindings.get("dataset") if isinstance(bindings, dict) else None
    source_id = binding.get("node_id") if isinstance(binding, dict) else None
    source = session.scalar(
        select(NodeRun).where(NodeRun.run_id == run.id, NodeRun.node_id == source_id)
    )
    version_id = _config(source).get("dataset_version_id") if source is not None else None
    if not isinstance(version_id, str):
        raise RuntimeError("Split source has no immutable dataset-version binding.")
    return version_id


def _config(node: NodeRun) -> dict[str, Any]:
    """Return public node configuration, excluding compiler-owned plan metadata."""
    return {key: value for key, value in node.resolved_config.items() if not key.startswith("__")}


def _input_artifact(session: Session, run: Run, node: NodeRun, port: str) -> Artifact:
    source, source_port = _input_source(session, run, node, port)
    output_ports = source.resolved_config.get("__outputs")
    if not isinstance(output_ports, list) or source_port not in output_ports:
        raise RuntimeError(f"Input {port!r} references an unknown source output.")
    index = output_ports.index(source_port)
    if index >= len(source.output_artifact_ids):
        raise RuntimeError(f"Source output for {port!r} was not produced.")
    artifact = session.get(Artifact, source.output_artifact_ids[index])
    if artifact is None or artifact.group_id != _run_group_id(session, run):
        raise RuntimeError(f"Input {port!r} does not reference an accessible artifact.")
    return artifact


def _input_source_node(session: Session, run: Run, node: NodeRun, port: str) -> NodeRun:
    return _input_source(session, run, node, port)[0]


def _input_source(session: Session, run: Run, node: NodeRun, port: str) -> tuple[NodeRun, str]:
    bindings = node.resolved_config.get("__inputs")
    if not isinstance(bindings, dict):
        raise RuntimeError(f"{node.node_type} execution plan has no input bindings.")
    binding = bindings.get(port)
    if not isinstance(binding, dict):
        raise RuntimeError(f"{node.node_type} is missing an input binding for {port!r}.")
    source_id, source_port = binding.get("node_id"), binding.get("port")
    if not isinstance(source_id, str) or not isinstance(source_port, str):
        raise RuntimeError(f"{node.node_type} has a malformed input binding for {port!r}.")
    source = session.scalar(
        select(NodeRun).where(NodeRun.run_id == run.id, NodeRun.node_id == source_id)
    )
    if source is None:
        raise RuntimeError(f"Input {port!r} references a missing node run.")
    return source, source_port


def _classifier_from_spec(spec: dict[str, Any]) -> Any:
    if spec.get("kind") != "logistic_regression":
        raise ValueError("Classifier specification is unsupported.")
    max_iter, seed = spec.get("max_iter"), spec.get("seed")
    if not isinstance(max_iter, int) or max_iter < 1 or not isinstance(seed, int):
        raise ValueError("Classifier specification is malformed.")
    try:
        from sklearn.linear_model import LogisticRegression  # type: ignore[import-untyped]
    except ImportError as error:  # pragma: no cover - QuaPy installation supplies scikit-learn
        raise RuntimeError("scikit-learn is required to execute classifier nodes.") from error
    return LogisticRegression(max_iter=max_iter, random_state=seed)


def _report_html(evaluation: dict[str, Any], *, run: Run, nodes: list[NodeRun]) -> str:
    timing_rows = "".join(
        "<tr>"
        f"<td>{html.escape(node.node_id)}</td>"
        f"<td>{html.escape(node.node_type)}</td>"
        f"<td>{html.escape(node.state)}</td>"
        f"<td>{_node_duration(node)}</td>"
        "</tr>"
        for node in sorted(
            nodes, key=lambda item: item.started_at.isoformat() if item.started_at else ""
        )
    )
    provenance = html.escape(json.dumps(run.environment, sort_keys=True, indent=2))
    if evaluation.get("schema_version") == 2:
        metric = "MAE"
        value = evaluation.get("metrics", {}).get("mae", "unavailable")
        samples = evaluation.get("samples", [])
        metric_rows = "".join(
            f"<tr><td>{html.escape(str(name).upper())}</td>"
            f"<td>{html.escape(str(metric_value))}</td></tr>"
            for name, metric_value in evaluation.get("metrics", {}).items()
        )
        sample_rows = "".join(
            "<tr>"
            f"<td>{html.escape(str(sample.get('sample_id', '')))}</td>"
            f"<td>{html.escape(json.dumps(sample.get('true_prevalence', [])))}</td>"
            f"<td>{html.escape(json.dumps(sample.get('estimated_prevalence', [])))}</td>"
            f"<td>{html.escape(str(sample.get('metrics', {}).get('mae', '')))}</td>"
            "</tr>"
            for sample in samples[:200]
            if isinstance(sample, dict)
        )
        truncation = (
            f"<p>Showing the first 200 of {len(samples)} samples; export CSV for all rows.</p>"
            if len(samples) > 200
            else ""
        )
        return (
            '<!doctype html><html lang="en"><head><meta charset="utf-8">'
            "<title>QuaPy Lab evaluation report</title>"
            "<style>body{font:15px system-ui;margin:2rem;color:#17251f;max-width:1100px}"
            "table{border-collapse:collapse;width:100%;margin:1rem 0}"
            "td,th{border:1px solid #b9c9bf;"
            "padding:.5rem;text-align:left;vertical-align:top}th{background:#edf5f0}"
            "pre{overflow:auto;padding:1rem;background:#f5f7f6}small{color:#627269}</style>"
            "</head><body><h1>QuaPy Lab evaluation report</h1>"
            "<p><strong>Protocol:</strong> "
            f"{html.escape(str(evaluation.get('protocol', 'unknown')).upper())}"
            " · <strong>Method:</strong> "
            f"{html.escape(str(evaluation.get('quantifier', 'unknown')).upper())}"
            f" · <strong>Samples:</strong> {len(samples)} · <strong>{metric}:</strong> "
            f"{html.escape(str(value))}</p>"
            "<p><strong>Classes:</strong> "
            f"{html.escape(', '.join(map(str, evaluation.get('classes', []))))}</p>"
            "<h2>Aggregate metrics</h2><table><thead><tr><th>Metric</th><th>Value</th>"
            f"</tr></thead><tbody>{metric_rows}</tbody></table>"
            "<h2>Per-sample evaluation</h2><table><thead><tr><th>Sample ID</th>"
            "<th>True prevalence</th><th>Estimated prevalence</th><th>MAE</th>"
            f"</tr></thead><tbody>{sample_rows}</tbody></table>{truncation}"
            "<h2>Node timings</h2><table><thead><tr><th>Node</th><th>Type</th>"
            f"<th>State</th><th>Wall time (s)</th></tr></thead><tbody>{timing_rows}</tbody></table>"
            f"<h2>Reproducibility</h2><p>Run <code>{html.escape(run.id)}</code>, seed "
            f"<code>{run.seed}</code>, workflow revision <code>"
            f"{html.escape(run.workflow_revision_id)}"
            f"</code>.</p><pre>{provenance}</pre></body></html>"
        )
    metric = html.escape(str(evaluation.get("metric", "metric")).upper())
    value = html.escape(str(evaluation.get("value", "unavailable")))
    classes = evaluation.get("classes", [])
    estimated = evaluation.get("estimated_prevalence", [])
    actual = evaluation.get("true_prevalence", [])
    rows = "".join(
        f"<tr><td>{html.escape(str(label))}</td><td>{html.escape(str(true))}</td>"
        f"<td>{html.escape(str(predicted))}</td></tr>"
        for label, true, predicted in zip(classes, actual, estimated, strict=True)
    )
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        "<title>QuaPy Lab evaluation report</title>"
        "<style>body{font:16px system-ui;margin:2rem;color:#17251f}table{border-collapse:collapse}"
        "td,th{border:1px solid #b9c9bf;padding:.5rem;text-align:left}</style></head><body>"
        "<h1>Evaluation report</h1>"
        "<p><strong>Method:</strong> "
        f"{html.escape(str(evaluation.get('quantifier', 'unknown')).upper())}"
        f" · <strong>{metric}:</strong> {value}</p>"
        "<table><thead><tr><th>Class</th><th>True prevalence</th>"
        f"<th>Estimated prevalence</th></tr></thead><tbody>{rows}</tbody></table>"
        "<h2>Node timings</h2><table><thead><tr><th>Node</th><th>Type</th>"
        f"<th>State</th><th>Wall time (s)</th></tr></thead><tbody>{timing_rows}</tbody></table>"
        f"<h2>Reproducibility</h2><p>Run <code>{html.escape(run.id)}</code>, seed "
        f"<code>{run.seed}</code>.</p><pre>{provenance}</pre></body></html>"
    )


def _node_duration(node: NodeRun) -> str:
    if node.started_at is None or node.ended_at is None:
        return ""
    return f"{(node.ended_at - node.started_at).total_seconds():.6f}"


def _run_group_id(session: Session, run: Run) -> str:
    project = session.get(Project, run.project_id)
    if project is None:
        raise RuntimeError("Run references a missing project.")
    return project.group_id


def _node(session: Session, run: Run, node_type: str, *, required: bool = True) -> NodeRun | None:
    node = session.scalar(
        select(NodeRun).where(NodeRun.run_id == run.id, NodeRun.node_type == node_type)
    )
    if node is None and required:
        raise RuntimeError(f"Run is missing required {node_type!r} node output.")
    return node


def _trusted_runtime_artifact(session: Session, run: Run, node: NodeRun) -> Artifact:
    if len(node.output_artifact_ids) != 1:
        raise RuntimeError(f"{node.node_type} did not produce a materialization artifact.")
    artifact = session.get(Artifact, node.output_artifact_ids[0])
    if (
        artifact is None
        or artifact.group_id != _run_group_id(session, run)
        or artifact.kind != "quapy-materialization"
        or artifact.media_type != _MATERIALIZATION_MEDIA_TYPE
        or artifact.serialization != {"format": "pickle", "trusted_producer": "workflow-runtime"}
    ):
        raise RuntimeError(f"{node.node_type} output is not a trusted runtime materialization.")
    return artifact


def _stratified_indices(
    data: Any, train_proportion: float, seed: int
) -> tuple[list[int], list[int]]:
    try:
        import numpy as np
    except ImportError as error:  # pragma: no cover - supplied by QuaPy
        raise RuntimeError("NumPy is required to create dataset splits.") from error
    labels = np.asarray(data.labels)
    random = np.random.RandomState(seed)
    train: list[int] = []
    test: list[int] = []
    for label in data.classes_:
        indices = np.flatnonzero(labels == label)
        random.shuffle(indices)
        count = int(round(len(indices) * train_proportion))
        if len(indices) > 1:
            count = min(max(count, 1), len(indices) - 1)
        train.extend(int(value) for value in indices[:count])
        test.extend(int(value) for value in indices[count:])
    random.shuffle(train)
    random.shuffle(test)
    if not train or not test:
        raise ValueError("Dataset is too small to create non-empty train and test partitions.")
    return train, test


def _class_distribution(data: Any, indices: list[int]) -> dict[str, int]:
    labels = data.labels
    return {
        str(label): sum(1 for index in indices if labels[index] == label) for label in data.classes_
    }


def _validate_prevalence(values: list[float], label: str) -> None:
    if (
        not values
        or any(not math.isfinite(value) or value < 0 for value in values)
        or not math.isclose(sum(values), 1.0, rel_tol=1e-7, abs_tol=1e-7)
    ):
        raise RuntimeError(
            f"The {label} prevalence vector must be finite, non-negative, and sum to one."
        )


def _evaluation_metrics(
    actual: list[float], estimated: list[float], sample_size: int
) -> dict[str, float]:
    try:
        import quapy.error as error  # type: ignore[import-untyped]
    except ImportError as import_error:  # pragma: no cover - deployment dependency failure
        raise RuntimeError("QuaPy is required to compute evaluation metrics.") from import_error
    epsilon = 1 / (2 * sample_size)
    return {
        "ae": float(error.ae(actual, estimated)),
        "rae": float(error.rae(actual, estimated, eps=epsilon)),
        "se": float(error.se(actual, estimated)),
        "kld": float(error.kld(actual, estimated, eps=epsilon)),
        "nkld": float(error.nkld(actual, estimated, eps=epsilon)),
        "nae": float(error.nae(actual, estimated)),
        "nrae": float(error.nrae(actual, estimated, eps=epsilon)),
    }


def _log(node: NodeRun, event: str, message: str) -> None:
    node.logs = [
        *node.logs,
        {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": "info",
            "node_id": node.node_id,
            "event": event,
            "message": message,
        },
    ]
