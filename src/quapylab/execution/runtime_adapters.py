"""Trusted, server-owned runtime adapters for the initial QuaPy workflow nodes.

The registry identifies adapters by the fixed ``(type, version)`` pair.  Workflow
documents never influence imports, file paths, or executable code.
"""

from __future__ import annotations

import csv
import io
import json
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


class RuntimeNodeExecutor:
    """Execute the small allowlisted runtime surface owned by QuaPy Lab."""

    def __init__(self, artifacts: ArtifactService) -> None:
        self._artifacts = artifacts
        self._adapters: dict[tuple[str, int], Adapter] = {
            ("dataset", 1): self._dataset,
            ("split", 1): self._split,
            ("classifier", 1): self._classifier,
            ("acc", 1): self._acc,
            ("evaluation", 1): self._evaluation,
        }

    def __call__(self, session: Session, run: Run, node: NodeRun) -> None:
        adapter = self._adapters.get((node.node_type, node.node_version))
        if adapter is None:
            raise RuntimeError(
                f"No runtime adapter is registered for {node.node_type!r} v{node.node_version}."
            )
        adapter(session, run, node)

    def _dataset(self, session: Session, run: Run, node: NodeRun) -> None:
        config = _dataset_config(node.resolved_config)
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
        data = self._dataset_from_previous_node(session, run)
        # Built-in QuaPy datasets are already delivered as deterministic train/test splits.
        if hasattr(data, "training") and hasattr(data, "test"):
            split_data = data
        else:
            train_proportion = node.resolved_config.get("train_proportion", 0.7)
            if not isinstance(train_proportion, (int, float)) or not 0 < train_proportion < 1:
                raise ValueError("Split train_proportion must be strictly between zero and one.")
            split_data = _split_collection(data, float(train_proportion), run.seed)
        artifact = self._store_materialization(session, run, node, split_data)
        node.output_artifact_ids = [artifact.id]
        _log(node, "split.completed", "Created the deterministic training and evaluation split.")

    def _classifier(self, session: Session, run: Run, node: NodeRun) -> None:
        data = self._dataset_from_previous_node(session, run)
        training = data.training if hasattr(data, "training") else data
        try:
            from sklearn.linear_model import LogisticRegression  # type: ignore[import-untyped]
        except ImportError as error:  # pragma: no cover - QuaPy installation supplies scikit-learn
            raise RuntimeError("scikit-learn is required to execute classifier nodes.") from error
        max_iter = node.resolved_config.get("max_iter", 1000)
        seed = node.resolved_config.get("seed", run.seed)
        if not isinstance(max_iter, int) or max_iter < 1 or not isinstance(seed, int):
            raise ValueError("Classifier max_iter and seed must be integers.")
        classifier = LogisticRegression(max_iter=max_iter, random_state=seed)
        classifier.fit(training.instances, training.labels)
        artifact = self._artifacts.create(
            session,
            group_id=_run_group_id(session, run),
            producing_run_id=run.id,
            producing_node_run_id=node.id,
            kind="sklearn-classifier",
            media_type="application/vnd.quapylab.sklearn-classifier+pickle",
            content=pickle.dumps(classifier, protocol=pickle.HIGHEST_PROTOCOL),
            serialization={"format": "pickle", "trusted_producer": "workflow-runtime"},
        )
        node.output_artifact_ids = [artifact.id]
        _log(node, "classifier.fitted", "Fitted the configured logistic-regression classifier.")

    def _acc(self, session: Session, run: Run, node: NodeRun) -> None:
        data = self._dataset_from_previous_node(session, run)
        try:
            from quapy.method.aggregative import ACC  # type: ignore[import-untyped]
        except ImportError as error:  # pragma: no cover - deployment dependency failure
            raise RuntimeError("QuaPy is required to execute ACC nodes.") from error
        quantifier = ACC()
        training = data.training if hasattr(data, "training") else data
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
        _log(node, "acc.fitted", "Fitted ACC quantifier on the training split.")

    def _evaluation(self, session: Session, run: Run, node: NodeRun) -> None:
        data = self._dataset_from_previous_node(session, run)
        quantifier = self._quantifier_from_previous_node(session, run)
        estimated = [float(value) for value in quantifier.quantify(data.test.instances)]
        actual = [float(value) for value in data.test.prevalence()]
        if len(estimated) != len(actual):
            raise RuntimeError("Quantifier output does not match the dataset class cardinality.")
        mae = sum(
            abs(left - right) for left, right in zip(estimated, actual, strict=True)
        ) / len(actual)
        result = {
            "schema_version": 1,
            "metric": "mae",
            "value": mae,
            "estimated_prevalence": estimated,
            "true_prevalence": actual,
            "classes": [str(value) for value in data.test.classes_],
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

    def _dataset_from_previous_node(self, session: Session, run: Run) -> Any:
        split = _node(session, run, "split", required=False)
        if split is not None and split.output_artifact_ids:
            return _load_pickle(self._artifacts, _trusted_runtime_artifact(session, run, split))
        return self._dataset_from_dataset_node(session, run)

    def _dataset_from_dataset_node(self, session: Session, run: Run) -> Any:
        source = _node(session, run, "dataset")
        assert source is not None
        config = _dataset_config(source.resolved_config)
        version = session.get(DatasetVersion, config.version_id)
        if version is None:
            raise RuntimeError("Dataset node references a missing dataset version.")
        artifact = _dataset_output_artifact(session, run, source)
        if not source.output_artifact_ids or source.output_artifact_ids != [artifact.id]:
            raise RuntimeError("Dataset node output does not match its selected dataset version.")
        return _load_pickle(self._artifacts, artifact)

    def _quantifier_from_previous_node(self, session: Session, run: Run) -> Any:
        source = _node(session, run, "acc")
        assert source is not None
        if len(source.output_artifact_ids) != 1:
            raise RuntimeError("ACC node did not produce a quantifier artifact.")
        artifact = session.get(Artifact, source.output_artifact_ids[0])
        if (
            artifact is None
            or artifact.group_id != _run_group_id(session, run)
            or artifact.kind != "quapy-quantifier"
            or artifact.media_type != _QUANTIFIER_MEDIA_TYPE
            or artifact.serialization
            != {"format": "pickle", "trusted_producer": "workflow-runtime"}
        ):
            raise RuntimeError("ACC output is not a trusted QuaPy runtime artifact.")
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


def _split_collection(data: Any, train_proportion: float, seed: int) -> Any:
    training, test = data.split_stratified(train_prop=train_proportion, random_state=seed)
    return _DatasetSplit(training=training, test=test)


class _DatasetSplit:
    def __init__(self, *, training: Any, test: Any) -> None:
        self.training = training
        self.test = test


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
