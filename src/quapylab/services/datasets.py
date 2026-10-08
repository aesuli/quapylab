"""CSV/TSV ingestion and immutable dataset-version persistence."""

from __future__ import annotations

import csv
import io
import pickle
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from quapylab.domain.actor import ActorContext
from quapylab.persistence.models import Dataset, DatasetVersion
from quapylab.services.artifacts import ArtifactService
from quapylab.services.authorization import AuthorizationError
from quapylab.services.projects import ProjectService


class DatasetService:
    def __init__(self, artifacts: ArtifactService) -> None:
        self._projects = ProjectService()
        self._artifacts = artifacts

    def ingest(
        self,
        session: Session,
        *,
        actor: ActorContext,
        project_id: str,
        name: str,
        filename: str,
        content: bytes,
        label_column: str | None = None,
    ) -> DatasetVersion:
        project = self._projects._project_for_edit(session, actor, project_id)
        if not name.strip() or not filename:
            raise ValueError("Dataset name and source filename are required.")
        delimiter = "\t" if filename.lower().endswith(".tsv") else ","
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError as error:
            raise ValueError("Dataset files must be UTF-8 encoded CSV or TSV.") from error
        reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
        if not reader.fieldnames or any(not item or not item.strip() for item in reader.fieldnames):
            raise ValueError("Dataset must contain a header row with non-empty column names.")
        columns = [item.strip() for item in reader.fieldnames]
        if len(set(columns)) != len(columns):
            raise ValueError("Dataset column names must be unique.")
        if label_column is not None and label_column not in columns:
            raise ValueError("The label column does not exist in the dataset.")
        rows = [{key: (value or "") for key, value in row.items()} for row in reader]
        if not rows:
            raise ValueError("Dataset must contain at least one data row.")
        schema = {"columns": [_column(name, rows) for name in columns]}
        classes = (
            sorted({row[label_column] for row in rows if row[label_column]}) if label_column else []
        )
        source_artifact = self._artifacts.create(
            session,
            group_id=project.group_id,
            kind="dataset-source",
            media_type="text/tab-separated-values" if delimiter == "\t" else "text/csv",
            content=content,
            serialization={"format": "tsv" if delimiter == "\t" else "csv", "filename": filename},
        )
        dataset = Dataset(
            project_id=project.id, name=name.strip(), is_labelled=label_column is not None
        )
        session.add(dataset)
        session.flush()
        version = DatasetVersion(
            dataset_id=dataset.id,
            schema=schema,
            row_count=len(rows),
            classes=classes,
            source={
                "filename": filename,
                "format": "tsv" if delimiter == "\t" else "csv",
                "artifact_id": source_artifact.id,
            },
            artifact_hashes=[source_artifact.sha256],
            ingestion_report={"preview": rows[:100], "label_column": label_column},
        )
        session.add(version)
        session.flush()
        dataset.current_version_id = version.id
        return version

    @staticmethod
    def quapy_sources() -> list[dict[str, Any]]:
        from quapy.data import datasets  # type: ignore[import-untyped]

        return [
            {
                "id": "reviews",
                "label": "Reviews sentiment",
                "datasets": datasets.REVIEWS_SENTIMENT_DATASETS,
                "options": {"tfidf": "boolean", "min_df": "integer|null"},
            },
            {
                "id": "twitter",
                "label": "Twitter sentiment",
                "datasets": datasets.TWITTER_SENTIMENT_DATASETS_TEST,
                "options": {"for_model_selection": "boolean", "min_df": "integer|null"},
            },
            {
                "id": "uci_binary",
                "label": "UCI binary",
                "datasets": datasets.UCI_BINARY_DATASETS,
                "options": {"test_split": "number", "standardize": "boolean"},
            },
            {
                "id": "uci_multiclass",
                "label": "UCI multiclass",
                "datasets": datasets.UCI_MULTICLASS_DATASETS,
                "options": {
                    "min_test_split": "number",
                    "max_train_instances": "integer",
                    "standardize": "boolean",
                    "min_class_support": "integer",
                },
            },
        ]

    def ingest_quapy(
        self,
        session: Session,
        *,
        actor: ActorContext,
        project_id: str,
        source_id: str,
        dataset_name: str,
        options: dict[str, Any],
    ) -> DatasetVersion:
        project = self._projects._project_for_edit(session, actor, project_id)
        source = next((item for item in self.quapy_sources() if item["id"] == source_id), None)
        if source is None or dataset_name not in source["datasets"]:
            raise ValueError("Unknown QuaPy dataset source or dataset name.")
        normalized = _normalize_options(source["options"], options)
        data = _load_quapy(source_id, dataset_name, normalized)
        train, test = data.training, data.test
        classes = [str(item) for item in train.classes_]
        schema = {
            "columns": _quapy_columns(train.instances),
            "splits": {"training_rows": len(train), "test_rows": len(test)},
            "representation": _quapy_representation(train.instances),
        }
        materialization = self._artifacts.create(
            session,
            group_id=project.group_id,
            kind="quapy-materialization",
            media_type="application/vnd.quapylab.quapy-materialization+pickle",
            content=pickle.dumps(data, protocol=pickle.HIGHEST_PROTOCOL),
            serialization={"format": "pickle", "trusted_producer": "quapy-loader"},
        )
        dataset = Dataset(
            project_id=project.id,
            name=_quapy_dataset_display_name(source_id, dataset_name, normalized),
            is_labelled=True,
        )
        session.add(dataset)
        session.flush()
        version = DatasetVersion(
            dataset_id=dataset.id,
            schema=schema,
            row_count=len(train) + len(test),
            classes=classes,
            source={
                "provider": "quapy",
                "quapy_version": __import__("quapy").__version__,
                "loader": source_id,
                "dataset": dataset_name,
                "options": normalized,
                "representation": _quapy_representation(train.instances),
                "artifact_id": materialization.id,
            },
            artifact_hashes=[materialization.sha256],
            ingestion_report={
                "preview": [],
                "splits": {"training_rows": len(train), "test_rows": len(test)},
            },
        )
        session.add(version)
        session.flush()
        dataset.current_version_id = version.id
        return version

    def list_datasets(
        self, session: Session, *, actor: ActorContext, project_id: str
    ) -> list[Dataset]:
        self._projects.get_project(session, actor=actor, project_id=project_id)
        return list(
            session.scalars(
                select(Dataset)
                .where(Dataset.project_id == project_id, Dataset.deleted_at.is_(None))
                .order_by(Dataset.name)
            )
        )

    def get_version(
        self, session: Session, *, actor: ActorContext, version_id: str
    ) -> DatasetVersion:
        version = session.get(DatasetVersion, version_id)
        if version is None:
            raise AuthorizationError("Resource not found.")
        dataset = session.get(Dataset, version.dataset_id)
        if dataset is None or dataset.deleted_at is not None:
            raise AuthorizationError("Resource not found.")
        self._projects.get_project(session, actor=actor, project_id=dataset.project_id)
        return version

    def delete(self, session: Session, *, actor: ActorContext, dataset_id: str) -> None:
        dataset = session.get(Dataset, dataset_id)
        if dataset is None or dataset.deleted_at is not None:
            raise AuthorizationError("Resource not found.")
        self._projects._project_for_edit(session, actor, dataset.project_id)
        dataset.deleted_at = datetime.now(UTC)


def _column(name: str, rows: list[dict[str, str]]) -> dict[str, Any]:
    values = [row[name] for row in rows if row[name] != ""]
    kind = "string"
    if values and all(_is_int(value) for value in values):
        kind = "integer"
    elif values and all(_is_number(value) for value in values):
        kind = "number"
    return {"name": name, "type": kind, "nullable": len(values) != len(rows)}


def _is_int(value: str) -> bool:
    try:
        int(value)
        return True
    except ValueError:
        return False


def _is_number(value: str) -> bool:
    try:
        float(value)
        return True
    except ValueError:
        return False


def _normalize_options(schema: dict[str, str], options: dict[str, Any]) -> dict[str, Any]:
    if set(options) - set(schema):
        raise ValueError("Unsupported option for this QuaPy dataset source.")
    normalized = dict(options)
    split_key = "test_split" if "test_split" in schema else "min_test_split"
    if split_key in schema:
        normalized.setdefault(split_key, 0.3)
        if not isinstance(normalized[split_key], (int, float)) or not 0 < normalized[split_key] < 1:
            raise ValueError(f"{split_key} must be a number strictly between zero and one.")
    for key, kind in schema.items():
        if kind == "boolean" and key in normalized and not isinstance(normalized[key], bool):
            raise ValueError(f"{key} must be a boolean.")
        if kind.startswith("integer") and key in normalized and normalized[key] is not None:
            if not isinstance(normalized[key], int) or isinstance(normalized[key], bool):
                raise ValueError(f"{key} must be an integer.")
    return normalized


def _load_quapy(source_id: str, dataset_name: str, options: dict[str, Any]) -> Any:
    from quapy.data import datasets

    if source_id == "reviews":
        return datasets.fetch_reviews(dataset_name, **options)
    if source_id == "twitter":
        return datasets.fetch_twitter(dataset_name, **options)
    if source_id == "uci_binary":
        return datasets.fetch_UCIBinaryDataset(dataset_name, **options)
    return datasets.fetch_UCIMulticlassDataset(dataset_name, **options)


def _quapy_dataset_display_name(source_id: str, dataset_name: str, options: dict[str, Any]) -> str:
    """Keep source variants distinguishable in project-level dataset selectors."""
    if not options:
        return f"{dataset_name} · {source_id}"
    rendered_options = ", ".join(f"{key}={options[key]!r}" for key in sorted(options))
    return f"{dataset_name} · {source_id} · {rendered_options}"


def _quapy_columns(instances: Any) -> list[dict[str, Any]]:
    shape = getattr(instances, "shape", None)
    if shape is not None and len(shape) > 1:
        return [
            {"name": f"feature_{index}", "type": "number", "nullable": False}
            for index in range(shape[1])
        ]
    return [{"name": "document", "type": "string", "nullable": False}]


def _quapy_representation(instances: Any) -> str:
    shape = getattr(instances, "shape", None)
    return "precomputed_matrix" if shape is not None and len(shape) > 1 else "text"
