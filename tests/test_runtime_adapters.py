"""Focused execution coverage using a small deterministic QuaPy stand-in."""

import json
import pickle
import sys
from io import BytesIO
from types import ModuleType

from quapylab.app import create_app
from quapylab.domain.actor import desktop_actor
from quapylab.execution.local_worker import LocalWorker
from quapylab.execution.runtime_adapters import RuntimeNodeExecutor
from quapylab.persistence.models import Artifact, Dataset, DatasetVersion, Project


class FixtureSplit:
    classes_ = ["negative", "positive"]
    instances = [0, 1]

    @staticmethod
    def prevalence() -> list[float]:
        return [0.5, 0.5]


class FixtureDataset:
    training = FixtureSplit()
    test = FixtureSplit()


class FixtureACC:
    def fit(self, _: FixtureSplit) -> "FixtureACC":
        return self

    def quantify(self, _: list[int]) -> list[float]:
        return [0.5, 0.5]


def _install_quapy(monkeypatch: object) -> None:
    quapy = ModuleType("quapy")
    method = ModuleType("quapy.method")
    aggregative = ModuleType("quapy.method.aggregative")
    aggregative.ACC = FixtureACC  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "quapy", quapy)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "quapy.method", method)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "quapy.method.aggregative", aggregative)  # type: ignore[attr-defined]


def _prepared_run(config: dict[str, object]) -> tuple[object, object, str]:
    application = create_app({"TESTING": True})
    client = application.test_client()
    group_id = str(desktop_actor().group_id)
    project_id = client.post(
        f"/api/v1/groups/{group_id}/projects", json={"name": "Experiment"}
    ).get_json()["id"]
    database = application.extensions["quapylab.database"]
    artifacts = application.extensions["quapylab.artifacts"]
    with database.transaction() as session:
        project = session.get(Project, project_id)
        assert project is not None
        dataset = Dataset(project_id=project.id, name="fixture", is_labelled=True)
        session.add(dataset)
        session.flush()
        materialization = artifacts.create(
            session,
            group_id=project.group_id,
            kind="quapy-materialization",
            media_type="application/vnd.quapylab.quapy-materialization+pickle",
            content=pickle.dumps(FixtureDataset()),
            serialization={"format": "pickle", "trusted_producer": "quapy-loader"},
        )
        version = DatasetVersion(
            dataset_id=dataset.id,
            schema={},
            row_count=4,
            classes=["negative", "positive"],
            source={"artifact_id": materialization.id},
            artifact_hashes=[materialization.sha256],
            ingestion_report={},
        )
        session.add(version)
        session.flush()
        dataset.current_version_id = version.id
        version_id = version.id
    graph = {
        "schema_version": 1,
        "nodes": [
            {"id": "dataset", "type": "dataset", "version": 1, "config": config},
            {"id": "acc", "type": "acc", "version": 1, "config": {}},
            {"id": "evaluation", "type": "evaluation", "version": 1, "config": {}},
        ],
        "edges": [
            {
                "id": "one",
                "source": "dataset",
                "source_port": "dataset",
                "target": "acc",
                "target_port": "train",
            },
            {
                "id": "two",
                "source": "acc",
                "source_port": "quantifier",
                "target": "evaluation",
                "target_port": "quantifier",
            },
        ],
    }
    if config == {"dataset_version_id": "VERSION"}:
        graph["nodes"][0]["config"] = {"dataset_version_id": version_id}
    workflow = client.post(
        f"/api/v1/projects/{project_id}/workflows", json={"name": "Fixture", "graph": graph}
    ).get_json()
    run = client.post(
        f"/api/v1/projects/{project_id}/runs",
        json={"workflow_revision_id": workflow["draft_revision_id"], "seed": 1},
    ).get_json()
    return application, client, run["id"]


def test_runtime_executes_and_persists_a_provenanced_evaluation(monkeypatch: object) -> None:
    _install_quapy(monkeypatch)
    application, client, run_id = _prepared_run({"dataset_version_id": "VERSION"})
    worker = LocalWorker(
        application.extensions["quapylab.database"],
        application.extensions["quapylab.runs"],
        RuntimeNodeExecutor(application.extensions["quapylab.artifacts"]),
    )

    assert worker.run_once()
    assert client.get(f"/api/v1/runs/{run_id}").get_json()["status"] == "succeeded"
    nodes = client.get(f"/api/v1/runs/{run_id}/nodes").get_json()["items"]
    evaluation = nodes[-1]
    artifact = client.get(f"/api/v1/artifacts/{evaluation['output_artifact_ids'][0]}").get_json()
    assert artifact["producing_run_id"] == run_id
    assert artifact["producing_node_run_id"] == evaluation["id"]
    download = client.get(f"/api/v1/artifacts/{artifact['id']}/download")
    assert json.loads(download.data)["value"] == 0.0


def test_runtime_rejects_missing_dataset_configuration() -> None:
    application, client, run_id = _prepared_run({})
    worker = LocalWorker(
        application.extensions["quapylab.database"],
        application.extensions["quapylab.runs"],
        RuntimeNodeExecutor(application.extensions["quapylab.artifacts"]),
    )

    assert worker.run_once()
    run = client.get(f"/api/v1/runs/{run_id}").get_json()
    assert run["status"] == "failed"
    assert "dataset_version_id" in run["error_summary"]


def test_runtime_verifies_materialization_integrity_before_unpickling() -> None:
    application, client, run_id = _prepared_run({"dataset_version_id": "VERSION"})
    database = application.extensions["quapylab.database"]
    artifacts = application.extensions["quapylab.artifacts"]
    with database.transaction() as session:
        version = session.query(DatasetVersion).one()
        artifact_id = version.source["artifact_id"]
    # The object-store corruption is deliberately outside the database transaction.
    # Look up the row through the public model instead of trusting a serialized input.
    with database.transaction() as session:
        artifact = session.get(Artifact, artifact_id)
        assert artifact is not None
        (artifacts._store.root / artifact.storage_key).write_bytes(b"corrupt")
    worker = LocalWorker(
        database,
        application.extensions["quapylab.runs"],
        RuntimeNodeExecutor(artifacts),
    )

    assert worker.run_once()
    run = client.get(f"/api/v1/runs/{run_id}").get_json()
    assert run["status"] == "failed"
    assert "integrity" in run["error_summary"]


def test_runtime_materializes_a_labelled_csv_and_executes_a_split() -> None:
    application = create_app({"TESTING": True})
    client = application.test_client()
    group_id = str(desktop_actor().group_id)
    project_id = client.post(
        f"/api/v1/groups/{group_id}/projects", json={"name": "CSV experiment"}
    ).get_json()["id"]
    rows = "\n".join(
        [f"{index},negative" for index in range(12)]
        + [f"{index + 100},positive" for index in range(12)]
    )
    dataset = client.post(
        f"/api/v1/projects/{project_id}/datasets",
        data={
            "name": "Numeric training data",
            "file": (BytesIO(f"feature,label\n{rows}\n".encode()), "training.csv"),
        },
    ).get_json()
    graph = {
        "schema_version": 1,
        "nodes": [
            {
                "id": "dataset",
                "type": "dataset",
                "version": 1,
                "config": {"dataset_version_id": dataset["id"], "label_column": "label"},
            },
            {
                "id": "split",
                "type": "split",
                "version": 1,
                "config": {"train_proportion": 0.7},
            },
            {"id": "acc", "type": "acc", "version": 1, "config": {}},
            {"id": "evaluation", "type": "evaluation", "version": 1, "config": {}},
        ],
        "edges": [
            {
                "id": "dataset-split",
                "source": "dataset",
                "source_port": "dataset",
                "target": "split",
                "target_port": "dataset",
            },
            {
                "id": "split-acc",
                "source": "split",
                "source_port": "train",
                "target": "acc",
                "target_port": "train",
            },
            {
                "id": "acc-evaluation",
                "source": "acc",
                "source_port": "quantifier",
                "target": "evaluation",
                "target_port": "quantifier",
            },
        ],
    }
    workflow = client.post(
        f"/api/v1/projects/{project_id}/workflows", json={"name": "CSV ACC", "graph": graph}
    ).get_json()
    run = client.post(
        f"/api/v1/projects/{project_id}/runs",
        json={"workflow_revision_id": workflow["draft_revision_id"], "seed": 7},
    ).get_json()
    worker = LocalWorker(
        application.extensions["quapylab.database"],
        application.extensions["quapylab.runs"],
        RuntimeNodeExecutor(application.extensions["quapylab.artifacts"]),
    )

    assert worker.run_once()
    result = client.get(f"/api/v1/runs/{run['id']}").get_json()
    assert result["status"] == "succeeded", result
