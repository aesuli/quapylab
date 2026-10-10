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
_QUANTIFIER_SPEC_MEDIA_TYPE = "application/vnd.quapylab.quantifier-spec+json"
_SPLIT_MEDIA_TYPE = "application/vnd.quapylab.dataset-split+json"
_PROTOCOL_MEDIA_TYPE = "application/vnd.quapylab.protocol+json"
_TRANSFORMER_MEDIA_TYPE = "application/vnd.quapylab.sklearn-transformer+pickle"


class RuntimeNodeExecutor:
    """Execute the small allowlisted runtime surface owned by QuaPy Lab."""

    def __init__(self, artifacts: ArtifactService) -> None:
        self._artifacts = artifacts
        self._adapters: dict[tuple[str, int], Adapter] = {
            ("dataset", 1): self._dataset,
            ("split", 1): self._split,
            ("logistic_regression", 1): self._classifier_algorithm,
            ("random_forest", 1): self._classifier_algorithm,
            ("preprocessing", 1): self._preprocessing,
            ("evaluation", 1): self._evaluation,
            ("app", 1): self._app,
            ("npp", 1): self._npp,
            ("protocol_evaluation", 1): self._protocol_evaluation,
            ("report", 1): self._report,
            ("comparison", 1): self._comparison,
            ("model_selection", 1): self._model_selection,
            ("fit_quantifier", 1): self._fit_quantifier_specification,
            **{
                (method, 1): self._method_quantifier_specification
                for method in ("acc", "pcc", "pacc", "emq", "hdy", "dmy")
            },
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
        data = _load_pickle(self._artifacts, artifact)
        if hasattr(data, "training") and hasattr(data, "test"):
            all_data = data.training + data.test
            outputs = (
                self._store_materialization(session, run, node, all_data),
                self._store_materialization(session, run, node, data.training),
                self._store_materialization(session, run, node, data.test),
            )
            node.output_artifact_ids = [output.id for output in outputs]
        else:
            node.output_artifact_ids = [artifact.id]
        _log(node, "dataset.loaded", "Loaded immutable trusted QuaPy materialization.")

    def _split(self, session: Session, run: Run, node: NodeRun) -> None:
        data = self._collection_input(session, run, node, "dataset")
        source = _input_artifact(session, run, node, "dataset")
        version_id = _source_dataset_version_id(session, run, node)
        train_proportion = _config(node).get("train_proportion", 0.7)
        validation_proportion = _config(node).get("validation_proportion", 0)
        if (
            not isinstance(train_proportion, (int, float))
            or isinstance(train_proportion, bool)
            or not isinstance(validation_proportion, (int, float))
            or isinstance(validation_proportion, bool)
            or not 0 < train_proportion < 1
            or not 0 <= validation_proportion < 1
            or train_proportion + validation_proportion >= 1
        ):
            raise ValueError(
                "Split proportions must leave non-empty train, validation, and test shares."
            )
        train_proportion = float(train_proportion)
        validation_proportion = float(validation_proportion)
        train_indices, validation_indices, test_indices = _stratified_partition_indices(
            data, train_proportion, validation_proportion, run.seed
        )
        common = {
            "schema_version": 1,
            "source_dataset_version_id": version_id,
            "source_artifact_id": source.id,
            "source_artifact_sha256": source.sha256,
            "seed": run.seed,
            "strategy": "stratified",
            "train_proportion": train_proportion,
            "validation_proportion": validation_proportion,
            "train_indices": train_indices,
            "validation_indices": validation_indices,
            "test_indices": test_indices,
            "class_distribution": {
                "train": _class_distribution(data, train_indices),
                "validation": _class_distribution(data, validation_indices),
                "test": _class_distribution(data, test_indices),
            },
        }
        train = self._store_split_manifest(session, run, node, {**common, "partition": "train"})
        test = self._store_split_manifest(session, run, node, {**common, "partition": "test"})
        validation = (
            self._store_split_manifest(
                session, run, node, {**common, "partition": "validation"}
            )
            if validation_indices
            else None
        )
        node.output_artifact_ids = [train.id, test.id]
        if validation is not None:
            node.output_artifact_ids.append(validation.id)
        _log(node, "split.completed", "Recorded deterministic train/validation/test partitions.")

    def _classifier_algorithm(self, session: Session, run: Run, node: NodeRun) -> None:
        spec = _classifier_spec({**_config(node), "kind": node.node_type}, run.seed)
        content = json.dumps(
            spec,
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
        _log(node, "classifier.resolved", f"Resolved the {spec['kind']} classifier.")

    def _preprocessing(self, session: Session, run: Run, node: NodeRun) -> None:
        training = self._collection_input(session, run, node, "train")
        test = self._collection_input(session, run, node, "test")
        transformer, resolved = _tabular_transformer(_config(node))
        try:
            transformed_train = transformer.fit_transform(training.instances)
            transformed_test = transformer.transform(test.instances)
            from quapy.data.base import LabelledCollection  # type: ignore[import-untyped]
        except (TypeError, ValueError) as error:
            raise ValueError(
                "Tabular preprocessing requires compatible two-dimensional numeric inputs."
            ) from error
        training_output = LabelledCollection(
            transformed_train, training.labels, classes=training.classes_
        )
        test_output = LabelledCollection(transformed_test, test.labels, classes=test.classes_)
        train_artifact = self._store_materialization(session, run, node, training_output)
        test_artifact = self._store_materialization(session, run, node, test_output)
        transformer_artifact = self._artifacts.create(
            session,
            group_id=_run_group_id(session, run),
            producing_run_id=run.id,
            producing_node_run_id=node.id,
            kind="fitted-tabular-transformer",
            media_type=_TRANSFORMER_MEDIA_TYPE,
            content=pickle.dumps(transformer, protocol=pickle.HIGHEST_PROTOCOL),
            serialization={
                "format": "pickle",
                "trusted_producer": "workflow-runtime",
                "resolved_config": resolved,
                "fit_scope": "train_only",
            },
        )
        node.output_artifact_ids = [
            train_artifact.id,
            test_artifact.id,
            transformer_artifact.id,
        ]
        _log(
            node,
            "preprocessing.completed",
            "Fitted preprocessing on training data and transformed both partitions.",
        )

    def _method_quantifier_specification(self, session: Session, run: Run, node: NodeRun) -> None:
        method = node.node_type
        classifier_spec = self._classifier_spec_input(session, run, node)
        options = _quantifier_options(method, _config(node))
        specification = {
            "schema_version": 1,
            "method": method,
            "classifier": classifier_spec,
            "options": options,
        }
        artifact = self._artifacts.create(
            session,
            group_id=_run_group_id(session, run),
            producing_run_id=run.id,
            producing_node_run_id=node.id,
            kind="quantifier-spec",
            media_type=_QUANTIFIER_SPEC_MEDIA_TYPE,
            content=json.dumps(specification, sort_keys=True, separators=(",", ":")).encode(),
            serialization={"format": "json", "schema_version": 1},
        )
        node.output_artifact_ids = [artifact.id]
        _log(node, "quantifier.resolved", f"Resolved unfitted {method.upper()} specification.")

    def _fit_quantifier_specification(self, session: Session, run: Run, node: NodeRun) -> None:
        training = self._collection_input(session, run, node, "train")
        specification = self._quantifier_spec_input(session, run, node)
        method = specification["method"]
        classifier = _classifier_from_spec(specification["classifier"])
        quantifier = _new_quantifier(method, classifier, specification["options"])
        self._fit_and_store_quantifier(
            session, run, node, training, quantifier, method.upper(), method=method
        )

    def _fit_and_store_quantifier(
        self,
        session: Session,
        run: Run,
        node: NodeRun,
        training: Any,
        quantifier: Any,
        method_name: str,
        *,
        method: str | None = None,
    ) -> None:
        labels = _quantifier_training_labels(training, method)
        if len(signature(quantifier.fit).parameters) >= 2:
            quantifier.fit(training.instances, labels)
        else:
            quantifier.fit(training)
        artifact = self._artifacts.create(
            session,
            group_id=_run_group_id(session, run),
            producing_run_id=run.id,
            producing_node_run_id=node.id,
            kind="quapy-quantifier",
            media_type=_QUANTIFIER_MEDIA_TYPE,
            content=pickle.dumps(quantifier, protocol=pickle.HIGHEST_PROTOCOL),
            serialization={
                "format": "pickle",
                "trusted_producer": "workflow-runtime",
                "method": method_name.lower(),
            },
        )
        node.output_artifact_ids = [artifact.id]
        _log(node, f"{method_name.lower()}.fitted", f"Fitted {method_name} quantifier.")

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
            "quantifier": self._quantifier_method_input(session, run, node),
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
            "quantifier": self._quantifier_method_input(session, run, node),
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

    def _comparison(self, session: Session, run: Run, node: NodeRun) -> None:
        evaluations: list[tuple[str, dict[str, Any]]] = []
        for port in ("evaluation_1", "evaluation_2", "evaluation_3", "evaluation_4"):
            artifact = _optional_input_artifact(session, run, node, port)
            if artifact is None:
                continue
            if (
                artifact.kind != "evaluation-result"
                or artifact.media_type != "application/vnd.quapylab.evaluation-result+json"
            ):
                raise RuntimeError(f"Comparison input {port!r} is not an evaluation result.")
            evaluation = json.loads(self._artifacts.read_for_worker(artifact))
            if not isinstance(evaluation, dict):
                raise RuntimeError(f"Comparison input {port!r} is malformed.")
            source = _input_source_node(session, run, node, port)
            evaluations.append((source.node_id, evaluation))
        comparison = _comparison_result(evaluations)
        comparison_artifact = self._artifacts.create(
            session,
            group_id=_run_group_id(session, run),
            producing_run_id=run.id,
            producing_node_run_id=node.id,
            kind="evaluation-comparison",
            media_type="application/vnd.quapylab.evaluation-comparison+json",
            content=json.dumps(comparison, sort_keys=True, separators=(",", ":")).encode(),
            serialization={"format": "json", "schema_version": 1},
        )
        report_artifact = self._artifacts.create(
            session,
            group_id=_run_group_id(session, run),
            producing_run_id=run.id,
            producing_node_run_id=node.id,
            kind="comparison-report",
            media_type="text/html",
            content=_comparison_html(comparison, run=run).encode("utf-8"),
            serialization={"format": "html", "schema_version": 1},
        )
        node.output_artifact_ids = [comparison_artifact.id, report_artifact.id]
        _log(
            node,
            "comparison.completed",
            f"Compared and ranked {len(evaluations)} methods on paired samples.",
        )

    def _model_selection(self, session: Session, run: Run, node: NodeRun) -> None:
        training = self._collection_input(session, run, node, "train")
        quantifier_spec = self._quantifier_spec_input(session, run, node)
        method = quantifier_spec["method"]
        classifier_spec = quantifier_spec["classifier"]
        protocol_artifact = _input_artifact(session, run, node, "validation_protocol")
        protocol, _classes = _load_protocol(
            session,
            run,
            protocol_artifact,
            self._artifacts,
            quantifier_method=method,
        )
        config = _model_selection_config(_config(node), classifier_spec)
        classifier = _classifier_from_spec(classifier_spec)
        try:
            from quapy.model_selection import GridSearchQ  # type: ignore[import-untyped]
        except ImportError as error:  # pragma: no cover - deployment dependency failure
            raise RuntimeError("QuaPy is required to execute model selection.") from error
        if method not in {"acc", "pcc", "pacc", "emq", "hdy", "dmy"}:
            raise ValueError(
                "Quantifier specification is unsupported by GridSearchQ."
            )
        if method == "hdy" and len(training.classes_) != 2:
            raise ValueError("HDy GridSearchQ requires a binary training collection.")
        quantifier = _new_quantifier(method, classifier, quantifier_spec["options"])
        search = GridSearchQ(
            quantifier,
            {config["parameter_name"]: config["classifier_values"]},
            protocol,
            error=config["error"],
            refit=config["refit"],
            timeout=config["timeout"],
            n_jobs=1,
            raise_errors=config["raise_errors"],
            verbose=config["verbose"],
        )
        search.fit(training.instances, _quantifier_training_labels(training, method))
        model_artifact = self._artifacts.create(
            session,
            group_id=_run_group_id(session, run),
            producing_run_id=run.id,
            producing_node_run_id=node.id,
            kind="quapy-quantifier",
            media_type=_QUANTIFIER_MEDIA_TYPE,
            content=pickle.dumps(search.best_model_, protocol=pickle.HIGHEST_PROTOCOL),
            serialization={
                "format": "pickle",
                "trusted_producer": "workflow-runtime",
                "selected_by": "GridSearchQ",
                "method": method,
            },
        )
        result = {
            "schema_version": 1,
            "method": method,
            "error": config["error"],
            "refit": config["refit"],
            "timeout": config["timeout"],
            "raise_errors": config["raise_errors"],
            "verbose": config["verbose"],
            "n_jobs": 1,
            "parameter_grid": {config["parameter_name"]: config["classifier_values"]},
            "best_params": search.best_params_,
            "best_score": float(search.best_score_),
            "fit_time_seconds": float(search.fit_time_),
            "attempts": {
                key: float(value) if isinstance(value, (int, float)) else str(value)
                for key, value in search.param_scores_.items()
            },
            "errors": [str(error) for error in search.error_collector],
        }
        evidence_artifact = self._artifacts.create(
            session,
            group_id=_run_group_id(session, run),
            producing_run_id=run.id,
            producing_node_run_id=node.id,
            kind="model-selection-result",
            media_type="application/vnd.quapylab.model-selection+json",
            content=json.dumps(result, sort_keys=True, separators=(",", ":")).encode(),
            serialization={"format": "json", "schema_version": 1},
        )
        node.output_artifact_ids = [model_artifact.id, evidence_artifact.id]
        _log(
            node,
            "model_selection.completed",
            f"Selected {method.upper()} parameters with {config['error'].upper()} "
            f"score {search.best_score_:.8g}.",
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

    def _quantifier_spec_input(self, session: Session, run: Run, node: NodeRun) -> dict[str, Any]:
        artifact = _input_artifact(session, run, node, "quantifier")
        if (
            artifact.kind != "quantifier-spec"
            or artifact.media_type != _QUANTIFIER_SPEC_MEDIA_TYPE
            or artifact.serialization != {"format": "json", "schema_version": 1}
        ):
            raise RuntimeError("Quantifier input is not a trusted quantifier specification.")
        result = json.loads(self._artifacts.read_for_worker(artifact))
        if (
            not isinstance(result, dict)
            or result.get("schema_version") != 1
            or not isinstance(result.get("method"), str)
            or not isinstance(result.get("classifier"), dict)
            or not isinstance(result.get("options"), dict)
        ):
            raise RuntimeError("Quantifier specification is malformed.")
        return result

    def _quantifier_input(self, session: Session, run: Run, node: NodeRun) -> Any:
        artifact = _input_artifact(session, run, node, "quantifier")
        serialization = artifact.serialization
        if (
            artifact.kind != "quapy-quantifier"
            or artifact.media_type != _QUANTIFIER_MEDIA_TYPE
            or serialization.get("format") != "pickle"
            or serialization.get("trusted_producer") != "workflow-runtime"
        ):
            raise RuntimeError("Quantifier input is not a trusted QuaPy runtime artifact.")
        return _load_pickle(self._artifacts, artifact)

    def _quantifier_method_input(self, session: Session, run: Run, node: NodeRun) -> str:
        artifact = _input_artifact(session, run, node, "quantifier")
        method = artifact.serialization.get("method")
        if not isinstance(method, str):
            raise RuntimeError("Fitted quantifier artifact does not identify its method.")
        return method

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
        from quapy.data.base import LabelledCollection
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
        matrix = [
            [float(row[feature]) if row[feature] != "" else np.nan for feature in features]
            for row in rows
        ]
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
    indices = (
        manifest.get(f"{partition}_indices")
        if partition in {"train", "validation", "test"}
        else None
    )
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


def _protocol_config(kind: str, config: dict[str, Any], run_seed: int) -> dict[str, Any]:
    allowed = {"sample_size", "repeats", "seed", "return_type"}
    if kind == "app":
        allowed.update({"n_prevalences", "smooth_limits_epsilon", "sanity_check"})
    if set(config) - allowed:
        raise ValueError(f"{kind.upper()} node config contains unsupported settings.")
    defaults: dict[str, Any] = {
        "sample_size": None,
        "repeats": 10 if kind == "app" else 100,
        "seed": run_seed,
        "return_type": "sample_prev",
    }
    if kind == "app":
        defaults.update({"n_prevalences": 21, "smooth_limits_epsilon": 0.0, "sanity_check": 10000})
    resolved = {**defaults, **config}
    sample_size = resolved["sample_size"]
    if resolved["return_type"] != "sample_prev":
        raise ValueError("Protocol return_type is fixed to sample_prev.")
    if sample_size is not None and (
        not isinstance(sample_size, int) or isinstance(sample_size, bool) or sample_size < 1
    ):
        raise ValueError("Protocol sample_size must be empty or a positive integer.")
    if (
        not isinstance(resolved["repeats"], int)
        or isinstance(resolved["repeats"], bool)
        or resolved["repeats"] < 1
        or not isinstance(resolved["seed"], int)
        or isinstance(resolved["seed"], bool)
    ):
        raise ValueError("Protocol repeats and seed must be integers, with positive repeats.")
    if kind == "app":
        epsilon = resolved["smooth_limits_epsilon"]
        if (
            not isinstance(resolved["n_prevalences"], int)
            or isinstance(resolved["n_prevalences"], bool)
            or resolved["n_prevalences"] < 2
            or not isinstance(epsilon, (int, float))
            or isinstance(epsilon, bool)
            or not 0 <= epsilon < 0.5
            or not isinstance(resolved["sanity_check"], int)
            or isinstance(resolved["sanity_check"], bool)
            or resolved["sanity_check"] < 1
        ):
            raise ValueError("APP prevalence, smoothing, or sanity-check settings are invalid.")
        resolved["smooth_limits_epsilon"] = float(epsilon)
    return resolved


def _load_protocol(
    session: Session,
    run: Run,
    artifact: Artifact,
    artifacts: ArtifactService,
    *,
    quantifier_method: str | None = None,
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
    original_classes = [str(value) for value in collection.classes_]
    if quantifier_method == "hdy":
        collection = _encoded_hdy_collection(collection)
    kind, config = specification.get("kind"), specification.get("config")
    if kind not in {"app", "npp"} or not isinstance(config, dict):
        raise RuntimeError("Protocol specification is malformed.")
    try:
        from quapy.protocol import APP, NPP  # type: ignore[import-untyped]
    except ImportError as error:  # pragma: no cover - deployment dependency failure
        raise RuntimeError("QuaPy is required to execute protocols.") from error
    protocol_type = APP if kind == "app" else NPP
    runtime_config = dict(config)
    # QuaPy falls back to its process-global SAMPLE_SIZE when this is None. The node
    # contract instead defines an empty value as the full connected collection size,
    # so resolve it locally and keep workflow execution independent of global state.
    if runtime_config.get("sample_size") is None:
        quapy_environment = run.environment.get("quapy_environment", {})
        project_sample_size = (
            quapy_environment.get("SAMPLE_SIZE") if isinstance(quapy_environment, dict) else None
        )
        runtime_config["sample_size"] = project_sample_size or len(collection)
    protocol = protocol_type(
        collection,
        random_state=runtime_config["seed"],
        **{key: value for key, value in runtime_config.items() if key != "seed"},
    )
    return protocol, original_classes


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


def _optional_input_artifact(
    session: Session, run: Run, node: NodeRun, port: str
) -> Artifact | None:
    bindings = node.resolved_config.get("__inputs")
    if not isinstance(bindings, dict) or port not in bindings:
        return None
    return _input_artifact(session, run, node, port)


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
    kind = spec.get("kind")
    try:
        from sklearn.ensemble import RandomForestClassifier  # type: ignore[import-untyped]
        from sklearn.linear_model import LogisticRegression  # type: ignore[import-untyped]
    except ImportError as error:  # pragma: no cover - QuaPy installation supplies scikit-learn
        raise RuntimeError("scikit-learn is required to execute classifier nodes.") from error
    if kind == "logistic_regression":
        return LogisticRegression(C=spec["c"], max_iter=spec["max_iter"], random_state=spec["seed"])
    if kind == "random_forest":
        return RandomForestClassifier(
            n_estimators=spec["n_estimators"],
            max_depth=spec["max_depth"],
            random_state=spec["seed"],
            n_jobs=1,
        )
    raise ValueError("Classifier specification is unsupported.")


def _classifier_spec(config: dict[str, Any], run_seed: int) -> dict[str, Any]:
    allowed = {"kind", "max_iter", "c", "n_estimators", "max_depth", "seed"}
    if set(config) - allowed:
        raise ValueError("Classifier node config contains unsupported settings.")
    kind = config.get("kind", "logistic_regression")
    seed = config.get("seed", run_seed)
    if not isinstance(seed, int) or isinstance(seed, bool):
        raise ValueError("Classifier seed must be an integer.")
    if kind == "logistic_regression":
        max_iter = config.get("max_iter", 1000)
        regularization = config.get("c", 1.0)
        if (
            not isinstance(max_iter, int)
            or isinstance(max_iter, bool)
            or max_iter < 1
            or not isinstance(regularization, (int, float))
            or isinstance(regularization, bool)
            or regularization <= 0
        ):
            raise ValueError("Logistic regression requires max_iter >= 1 and C > 0.")
        return {
            "kind": kind,
            "max_iter": max_iter,
            "c": float(regularization),
            "seed": seed,
        }
    if kind == "random_forest":
        n_estimators = config.get("n_estimators", 200)
        max_depth = config.get("max_depth")
        if (
            not isinstance(n_estimators, int)
            or isinstance(n_estimators, bool)
            or n_estimators < 1
            or (
                max_depth is not None
                and (not isinstance(max_depth, int) or isinstance(max_depth, bool) or max_depth < 1)
            )
        ):
            raise ValueError("Random forest requires n_estimators >= 1 and max_depth >= 1.")
        return {
            "kind": kind,
            "n_estimators": n_estimators,
            "max_depth": max_depth,
            "seed": seed,
        }
    raise ValueError("Classifier kind is not allow-listed.")


def _new_quantifier(method: str, classifier: Any, options: dict[str, Any]) -> Any:
    method_names = {
        "acc": "ACC",
        "pcc": "PCC",
        "pacc": "PACC",
        "emq": "EMQ",
        "hdy": "HDy",
        "dmy": "DMy",
    }
    class_name = method_names.get(method)
    if class_name is None:
        raise ValueError("Quantifier method is not allow-listed.")
    try:
        import quapy.method.aggregative as aggregative  # type: ignore[import-untyped]
    except ImportError as error:  # pragma: no cover - deployment dependency failure
        raise RuntimeError("QuaPy is required to construct quantifiers.") from error
    quantifier_type = getattr(aggregative, class_name, None)
    if quantifier_type is None:
        raise RuntimeError(f"Installed QuaPy does not provide {class_name}.")
    try:
        constructor = signature(quantifier_type).parameters
        resolved = {key: value for key, value in options.items() if key in constructor}
        return quantifier_type(classifier, **resolved)
    except TypeError:  # pragma: no cover - accommodates a QuaPy-compatible adapter surface
        quantifier = quantifier_type()
        quantifier.classifier = classifier
        return quantifier


def _quantifier_training_labels(training: Any, method: str | None) -> Any:
    if method != "hdy":
        return training.labels
    return _encoded_hdy_labels(training)


def _encoded_hdy_labels(collection: Any) -> Any:
    try:
        import numpy as np
    except ImportError as error:  # pragma: no cover - supplied by QuaPy
        raise RuntimeError("NumPy is required to encode HDy training labels.") from error
    class_indices = {label: index for index, label in enumerate(collection.classes_)}
    return np.asarray([class_indices[label] for label in collection.labels])


def _encoded_hdy_collection(collection: Any) -> Any:
    try:
        from quapy.data.base import LabelledCollection  # type: ignore[import-untyped]
    except ImportError as error:  # pragma: no cover - deployment dependency failure
        raise RuntimeError("QuaPy is required to encode the HDy validation collection.") from error
    return LabelledCollection(
        collection.instances,
        _encoded_hdy_labels(collection),
        classes=list(range(len(collection.classes_))),
    )


def _quantifier_options(method: str, config: dict[str, Any]) -> dict[str, Any]:
    defaults: dict[str, dict[str, Any]] = {
        "acc": {
            "fit_classifier": True,
            "n_jobs": 1,
            "val_split": 5,
            "solver": "minimize",
            "method": "inversion",
            "norm": "clip",
        },
        "pcc": {"fit_classifier": True, "val_split": None},
        "pacc": {
            "fit_classifier": True,
            "n_jobs": 1,
            "val_split": 5,
            "solver": "minimize",
            "method": "inversion",
            "norm": "clip",
        },
        "emq": {
            "fit_classifier": True,
            "n_jobs": 1,
            "val_split": None,
            "exact_train_prev": True,
            "calib": "none",
            "on_calib_error": "raise",
        },
        "hdy": {"fit_classifier": True, "val_split": 5},
        "dmy": {
            "fit_classifier": True,
            "n_jobs": 1,
            "val_split": 5,
            "nbins": 8,
            "divergence": "HD",
            "cdf": False,
            "search": "optim_minimize",
        },
    }
    if method not in defaults or set(config) - set(defaults[method]):
        raise ValueError(f"{method.upper()} config contains unsupported settings.")
    resolved = {**defaults[method], **config}
    if resolved["fit_classifier"] is not True:
        raise ValueError("Quantifier classifier fitting is required by this workflow contract.")
    if "n_jobs" in resolved and resolved["n_jobs"] != 1:
        raise ValueError("Quantifier n_jobs is fixed to one by the resource policy.")
    if "val_split" in resolved:
        val_split = resolved["val_split"]
        valid_split = (
            val_split is None
            or (isinstance(val_split, int) and not isinstance(val_split, bool) and val_split >= 2)
            or (isinstance(val_split, float) and math.isfinite(val_split) and 0 < val_split < 1)
        )
        if not valid_split:
            raise ValueError("val_split must be empty, at least two folds, or a fraction in (0,1).")
    if method in {"acc", "pacc"}:
        if resolved["solver"] not in {"minimize", "exact-raise", "exact-cc"}:
            raise ValueError("Adjustment solver is unsupported.")
        if resolved["method"] not in {"inversion", "invariant-ratio"}:
            raise ValueError("Adjustment method is unsupported.")
        if resolved["norm"] not in {"clip", "mapsimplex", "condsoftmax"}:
            raise ValueError("Adjustment normalization is unsupported.")
    elif method == "emq":
        if not isinstance(resolved["exact_train_prev"], bool):
            raise ValueError("EMQ exact_train_prev must be boolean.")
        if resolved["calib"] not in {"none", "nbvs", "bcts", "ts", "vs"}:
            raise ValueError("EMQ calibration is unsupported.")
        if resolved["on_calib_error"] not in {"raise", "backup"}:
            raise ValueError("EMQ calibration error policy is unsupported.")
        resolved["calib"] = None if resolved["calib"] == "none" else resolved["calib"]
    elif method == "dmy":
        if (
            not isinstance(resolved["nbins"], int)
            or isinstance(resolved["nbins"], bool)
            or resolved["nbins"] < 2
        ):
            raise ValueError("DMy nbins must be an integer of at least two.")
        if resolved["divergence"] not in {"HD", "topsoe"}:
            raise ValueError("DMy divergence is unsupported.")
        if not isinstance(resolved["cdf"], bool):
            raise ValueError("DMy cdf must be boolean.")
        if resolved["search"] not in {"optim_minimize", "linear_search", "ternary_search"}:
            raise ValueError("DMy search strategy is unsupported.")
    return resolved


def _model_selection_config(
    config: dict[str, Any], classifier_spec: dict[str, Any]
) -> dict[str, Any]:
    allowed = {
        "classifier_values",
        "error",
        "refit",
        "timeout",
        "raise_errors",
        "verbose",
        "n_jobs",
    }
    if set(config) - allowed:
        raise ValueError("Model-selection config contains unsupported settings.")
    values = config.get("classifier_values", [0.1, 1.0, 10.0])
    error = config.get("error", "mae")
    refit = config.get("refit", True)
    timeout = config.get("timeout", -1)
    raise_errors = config.get("raise_errors", False)
    verbose = config.get("verbose", False)
    n_jobs = config.get("n_jobs", 1)
    if error not in {"mae", "mrae", "mse", "mkld", "mnkld", "mnae", "mnrae"}:
        raise ValueError("Model-selection error measure is not allow-listed.")
    if not isinstance(refit, bool):
        raise ValueError("Model-selection refit must be boolean.")
    if (
        not isinstance(timeout, int)
        or isinstance(timeout, bool)
        or timeout < -1
        or not isinstance(raise_errors, bool)
        or not isinstance(verbose, bool)
        or n_jobs != 1
    ):
        raise ValueError("Model-selection execution policy is invalid.")
    if not isinstance(values, list) or not 1 <= len(values) <= 50:
        raise ValueError("Model selection requires between one and fifty classifier values.")
    classifier_kind = classifier_spec.get("kind")
    if classifier_kind == "logistic_regression":
        if any(
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(value)
            or value <= 0
            for value in values
        ):
            raise ValueError("Logistic-regression C candidates must be finite and positive.")
        parameter_name = "classifier__C"
        resolved_values: list[int | float] = [float(value) for value in values]
    elif classifier_kind == "random_forest":
        if any(
            not isinstance(value, int) or isinstance(value, bool) or value < 1 for value in values
        ):
            raise ValueError("Random-forest tree-count candidates must be positive integers.")
        parameter_name = "classifier__n_estimators"
        resolved_values = values
    else:
        raise ValueError("Classifier is unsupported for model selection.")
    return {
        "classifier_values": resolved_values,
        "parameter_name": parameter_name,
        "error": error,
        "refit": refit,
        "timeout": timeout,
        "raise_errors": raise_errors,
        "verbose": verbose,
        "n_jobs": 1,
    }


def _tabular_transformer(config: dict[str, Any]) -> tuple[Any, dict[str, str]]:
    allowed = {"imputation", "scaling"}
    if set(config) - allowed:
        raise ValueError("Preprocessing node config contains unsupported settings.")
    imputation = config.get("imputation", "none")
    scaling = config.get("scaling", "standard")
    if imputation not in {"none", "mean", "median", "most_frequent"}:
        raise ValueError("Preprocessing imputation strategy is unsupported.")
    if scaling not in {"none", "standard", "minmax", "robust"}:
        raise ValueError("Preprocessing scaling strategy is unsupported.")
    try:
        from sklearn.impute import SimpleImputer  # type: ignore[import-untyped]
        from sklearn.pipeline import Pipeline  # type: ignore[import-untyped]
        from sklearn.preprocessing import (  # type: ignore[import-untyped]
            FunctionTransformer,
            MinMaxScaler,
            RobustScaler,
            StandardScaler,
        )
    except ImportError as error:  # pragma: no cover - supplied by QuaPy
        raise RuntimeError("scikit-learn is required for preprocessing.") from error
    steps: list[tuple[str, Any]] = []
    if imputation != "none":
        steps.append(("imputer", SimpleImputer(strategy=imputation)))
    scaler_types = {
        "standard": StandardScaler,
        "minmax": MinMaxScaler,
        "robust": RobustScaler,
    }
    if scaling != "none":
        steps.append(("scaler", scaler_types[scaling]()))
    if not steps:
        steps.append(("identity", FunctionTransformer(validate=True)))
    return Pipeline(steps), {"imputation": imputation, "scaling": scaling}


def _comparison_result(
    evaluations: list[tuple[str, dict[str, Any]]],
) -> dict[str, Any]:
    if len(evaluations) < 2:
        raise ValueError("A comparison requires at least two evaluation results.")
    reference_id, reference = evaluations[0]
    if reference.get("schema_version") != 2 or not isinstance(reference.get("samples"), list):
        raise ValueError(
            "Method comparison requires protocol evaluation results with paired samples."
        )
    protocol = reference.get("protocol")
    classes = reference.get("classes")
    reference_samples = reference["samples"]
    sample_ids = [sample.get("sample_id") for sample in reference_samples]
    true_prevalences = [sample.get("true_prevalence") for sample in reference_samples]
    if (
        not isinstance(protocol, str)
        or not isinstance(classes, list)
        or not sample_ids
        or any(not isinstance(sample_id, str) for sample_id in sample_ids)
    ):
        raise ValueError(f"Evaluation {reference_id!r} has malformed pairing metadata.")
    methods: list[dict[str, Any]] = []
    for method_id, evaluation in evaluations:
        samples = evaluation.get("samples")
        if (
            evaluation.get("schema_version") != 2
            or evaluation.get("protocol") != protocol
            or evaluation.get("classes") != classes
            or not isinstance(samples, list)
            or [sample.get("sample_id") for sample in samples] != sample_ids
            or [sample.get("true_prevalence") for sample in samples] != true_prevalences
        ):
            raise ValueError(
                "Comparison inputs must use the same protocol, classes, paired sample IDs, "
                "and true prevalences."
            )
        metrics = evaluation.get("metrics")
        if not isinstance(metrics, dict) or not metrics:
            raise ValueError(f"Evaluation {method_id!r} has no aggregate metrics.")
        methods.append(
            {
                "method_id": method_id,
                "quantifier": str(evaluation.get("quantifier", method_id)),
                "metrics": {name: float(value) for name, value in metrics.items()},
                "samples": samples,
            }
        )
    metric_names = sorted(set.intersection(*(set(method["metrics"]) for method in methods)))
    rankings = {
        metric: [
            {"rank": rank, "method_id": method["method_id"], "value": method["metrics"][metric]}
            for rank, method in enumerate(
                sorted(methods, key=lambda item: (item["metrics"][metric], item["method_id"])),
                start=1,
            )
        ]
        for metric in metric_names
    }
    return {
        "schema_version": 1,
        "protocol": protocol,
        "classes": classes,
        "paired_sample_ids": sample_ids,
        "metric_names": metric_names,
        "rankings": rankings,
        "methods": methods,
    }


def _comparison_html(comparison: dict[str, Any], *, run: Run) -> str:
    methods = comparison["methods"]
    metric_names = comparison["metric_names"]
    ranking_rows = "".join(
        "<tr>"
        f"<td>{rank}</td><td>{html.escape(item['method_id'])}</td>"
        f"<td>{html.escape(str(item['quantifier']).upper())}</td>"
        + "".join(f"<td>{item['metrics'][metric]:.8g}</td>" for metric in metric_names)
        + "</tr>"
        for rank, item in enumerate(
            sorted(
                methods,
                key=lambda method: (
                    method["metrics"].get("mae", float("inf")),
                    method["method_id"],
                ),
            ),
            start=1,
        )
    )
    headers = "".join(f"<th>{html.escape(metric.upper())}</th>" for metric in metric_names)
    plots = _binary_comparison_plots(comparison)
    provenance = html.escape(json.dumps(run.environment, sort_keys=True, indent=2))
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        "<title>QuaPy Lab method comparison</title><style>"
        "body{font:15px system-ui;margin:2rem;color:#17251f;max-width:1200px}"
        "table{border-collapse:collapse;width:100%;margin:1rem 0}"
        "td,th{border:1px solid #b9c9bf;padding:.5rem;text-align:left}"
        "th{background:#edf5f0}svg{border:1px solid #b9c9bf;background:white;margin:.5rem}"
        "pre{overflow:auto;padding:1rem;background:#f5f7f6}</style></head><body>"
        "<h1>QuaPy Lab method comparison</h1>"
        f"<p><strong>Protocol:</strong> {html.escape(str(comparison['protocol']).upper())} · "
        f"<strong>Paired samples:</strong> {len(comparison['paired_sample_ids'])} · "
        f"<strong>Methods:</strong> {len(methods)}</p>"
        "<p>Ranks are descriptive and ordered by lower mean error; no inferential significance "
        "claim is made.</p><h2>Method ranking</h2>"
        f"<table><thead><tr><th>MAE rank</th><th>Node</th><th>Method</th>{headers}</tr>"
        f"</thead><tbody>{ranking_rows}</tbody></table>{plots}"
        f"<h2>Reproducibility</h2><p>Run <code>{html.escape(run.id)}</code>, seed "
        f"<code>{run.seed}</code>, workflow revision <code>{html.escape(run.workflow_revision_id)}"
        f"</code>.</p><pre>{provenance}</pre></body></html>"
    )


def _binary_comparison_plots(comparison: dict[str, Any]) -> str:
    if len(comparison["classes"]) != 2:
        return "<p>Binary diagnostic plots are omitted for multiclass results.</p>"
    colours = ("#13795b", "#a15c00", "#3659a2", "#9b3a62")
    diagonal_series: list[str] = []
    error_series: list[str] = []
    legend: list[str] = []
    for index, method in enumerate(comparison["methods"]):
        colour = colours[index % len(colours)]
        points = [
            (sample["true_prevalence"][1], sample["estimated_prevalence"][1])
            for sample in method["samples"]
        ]
        error_points = [
            (sample["true_prevalence"][1], sample["metrics"]["ae"]) for sample in method["samples"]
        ]
        diagonal_series.append(_svg_points(points, colour))
        error_series.append(_svg_points(error_points, colour))
        legend.append(
            f'<span style="color:{colour};margin-right:1rem">● '
            f"{html.escape(method['method_id'])} "
            f"({html.escape(str(method['quantifier']).upper())})</span>"
        )
    diagonal = _svg_plot(
        "True positive prevalence", "Estimated positive prevalence", diagonal_series, True
    )
    error_plot = _svg_plot("True positive prevalence", "Absolute error", error_series, False)
    return f"<h2>Binary diagnostics</h2><p>{''.join(legend)}</p>{diagonal}{error_plot}"


def _svg_points(points: list[tuple[float, float]], colour: str) -> str:
    return "".join(
        f'<circle cx="{45 + 300 * x:.3f}" cy="{325 - 280 * min(max(y, 0), 1):.3f}" '
        f'r="3" fill="{colour}" fill-opacity="0.7"/>'
        for x, y in points
    )


def _svg_plot(x_label: str, y_label: str, series: list[str], diagonal: bool) -> str:
    guide = '<line x1="45" y1="325" x2="345" y2="45" stroke="#89958e"/>' if diagonal else ""
    return (
        '<svg viewBox="0 0 380 370" width="380" height="370" role="img" '
        f'aria-label="{html.escape(y_label)} by {html.escape(x_label)}">'
        '<line x1="45" y1="325" x2="345" y2="325" stroke="#17251f"/>'
        '<line x1="45" y1="325" x2="45" y2="45" stroke="#17251f"/>'
        f"{guide}{''.join(series)}"
        f'<text x="195" y="360" text-anchor="middle">{html.escape(x_label)}</text>'
        f'<text x="14" y="185" text-anchor="middle" transform="rotate(-90 14 185)">'
        f"{html.escape(y_label)}</text></svg>"
    )


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


def _stratified_partition_indices(
    data: Any, train_proportion: float, validation_proportion: float, seed: int
) -> tuple[list[int], list[int], list[int]]:
    try:
        import numpy as np
    except ImportError as error:  # pragma: no cover - supplied by QuaPy
        raise RuntimeError("NumPy is required to create dataset splits.") from error
    labels = np.asarray(data.labels)
    random = np.random.RandomState(seed)
    train: list[int] = []
    validation: list[int] = []
    test: list[int] = []
    for label in data.classes_:
        indices = np.flatnonzero(labels == label)
        random.shuffle(indices)
        if validation_proportion > 0 and len(indices) < 3:
            raise ValueError(
                "Each class needs at least three rows for train/validation/test splitting."
            )
        train_count = int(round(len(indices) * train_proportion))
        validation_count = int(round(len(indices) * validation_proportion))
        reserved = 1 + (1 if validation_proportion > 0 else 0)
        train_count = min(max(train_count, 1), len(indices) - reserved)
        if validation_proportion > 0:
            validation_count = min(max(validation_count, 1), len(indices) - train_count - 1)
        else:
            validation_count = 0
        validation_end = train_count + validation_count
        train.extend(int(value) for value in indices[:train_count])
        validation.extend(int(value) for value in indices[train_count:validation_end])
        test.extend(int(value) for value in indices[validation_end:])
    random.shuffle(train)
    random.shuffle(validation)
    random.shuffle(test)
    if not train or not test:
        raise ValueError("Dataset is too small to create non-empty train and test partitions.")
    return train, validation, test


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
