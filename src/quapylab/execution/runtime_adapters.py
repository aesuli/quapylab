"""Trusted, server-owned runtime adapters for the initial QuaPy workflow nodes.

The registry identifies adapters by the fixed ``(type, version)`` pair.  Workflow
documents never influence imports, file paths, or executable code.
"""

from __future__ import annotations

import json
import pickle
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, cast

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
        version_id = _dataset_version_id(node.resolved_config)
        version = session.get(DatasetVersion, version_id)
        if version is None:
            raise ValueError("Dataset configuration references an unknown dataset version.")
        dataset = session.get(Dataset, version.dataset_id)
        if dataset is None or dataset.project_id != run.project_id:
            raise ValueError("Dataset configuration references a dataset outside this project.")
        artifact = _trusted_materialization(session, run, version)
        # Verify before deserializing, even though this node only passes the immutable input onward.
        _load_pickle(self._artifacts, artifact)
        node.output_artifact_ids = [artifact.id]
        _log(
            node,
            "info",
            "dataset.loaded",
            "Loaded immutable trusted QuaPy materialization.",
        )

    def _acc(self, session: Session, run: Run, node: NodeRun) -> None:
        data = self._dataset_from_previous_node(session, run)
        try:
            from quapy.method.aggregative import ACC  # type: ignore[import-untyped]
        except ImportError as error:  # pragma: no cover - deployment dependency failure
            raise RuntimeError("QuaPy is required to execute ACC nodes.") from error
        quantifier = ACC()
        quantifier.fit(data.training)
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
        _log(node, "info", "acc.fitted", "Fitted ACC quantifier on the training split.")

    def _evaluation(self, session: Session, run: Run, node: NodeRun) -> None:
        data = self._dataset_from_dataset_node(session, run)
        quantifier = self._quantifier_from_previous_node(session, run)
        estimated = [float(value) for value in quantifier.quantify(data.test.instances)]
        actual = [float(value) for value in data.test.prevalence()]
        if len(estimated) != len(actual):
            raise RuntimeError("Quantifier output does not match the dataset class cardinality.")
        mae = sum(abs(left - right) for left, right in zip(estimated, actual, strict=True)) / len(actual)
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
        _log(node, "info", "evaluation.completed", "Computed normalized ACC evaluation result.")

    def _dataset_from_previous_node(self, session: Session, run: Run) -> Any:
        return self._dataset_from_dataset_node(session, run)

    def _dataset_from_dataset_node(self, session: Session, run: Run) -> Any:
        source = _node(session, run, "dataset")
        version_id = _dataset_version_id(source.resolved_config)
        version = session.get(DatasetVersion, version_id)
        if version is None:
            raise RuntimeError("Dataset node references a missing dataset version.")
        artifact = _trusted_materialization(session, run, version)
        if source.output_artifact_ids != [artifact.id]:
            raise RuntimeError("Dataset node output does not match its selected dataset version.")
        return _load_pickle(self._artifacts, artifact)

    def _quantifier_from_previous_node(self, session: Session, run: Run) -> Any:
        source = _node(session, run, "acc")
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


def _dataset_version_id(config: dict[str, Any]) -> str:
    if set(config) != {"dataset_version_id"} or not isinstance(
        config.get("dataset_version_id"), str
    ) or not config["dataset_version_id"].strip():
        raise ValueError("Dataset node config must contain only a non-empty dataset_version_id.")
    return cast(str, config["dataset_version_id"])


def _trusted_materialization(session: Session, run: Run, version: DatasetVersion) -> Artifact:
    artifact_id = version.source.get("artifact_id")
    if not isinstance(artifact_id, str):
        raise ValueError("Dataset version has no QuaPy materialization artifact.")
    artifact = session.get(Artifact, artifact_id)
    if (
        artifact is None
        or artifact.group_id != _run_group_id(session, run)
        or artifact.sha256 not in version.artifact_hashes
        or artifact.kind != "quapy-materialization"
        or artifact.media_type != _MATERIALIZATION_MEDIA_TYPE
        or artifact.serialization != {"format": "pickle", "trusted_producer": "quapy-loader"}
    ):
        raise ValueError("Dataset version does not reference a trusted QuaPy materialization.")
    return artifact


def _load_pickle(artifacts: ArtifactService, artifact: Artifact) -> Any:
    # read_for_worker verifies SHA-256 immediately before the only deserialization step.
    return pickle.loads(artifacts.read_for_worker(artifact))


def _run_group_id(session: Session, run: Run) -> str:
    project = session.get(Project, run.project_id)
    if project is None:
        raise RuntimeError("Run references a missing project.")
    return project.group_id


def _node(session: Session, run: Run, node_type: str) -> NodeRun:
    node = session.scalar(
        select(NodeRun).where(NodeRun.run_id == run.id, NodeRun.node_type == node_type)
    )
    if node is None:
        raise RuntimeError(f"Run is missing required {node_type!r} node output.")
    return node


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
