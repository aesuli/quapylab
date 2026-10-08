import os
import subprocess
import time
from io import BytesIO
from pathlib import Path
from typing import Any

from quapylab.app import create_app
from quapylab.domain.actor import desktop_actor
from quapylab.execution.backends import ProcessRunBackend
from quapylab.persistence.models import Run


class FakeProcess:
    pid = 4321

    def __init__(self) -> None:
        self.terminated = False

    def poll(self) -> None:
        return None

    def terminate(self) -> None:
        self.terminated = True

    def wait(self, timeout: float | None = None) -> int:
        if timeout is not None:
            raise subprocess.TimeoutExpired("worker", timeout)
        return 0


class DormantThread:
    def __init__(self, **_: Any) -> None:
        pass

    def start(self) -> None:
        pass


class CancellationThread:
    def __init__(self, *, target: Any, args: tuple[Any, ...], name: str, **_: Any) -> None:
        self.target = target
        self.args = args
        self.name = name

    def start(self) -> None:
        if self.name.startswith("quapylab-cancel-"):
            self.target(*self.args)


def test_process_backend_launches_one_isolated_worker_per_run(
    tmp_path: Path, monkeypatch: Any
) -> None:
    launched: list[tuple[list[str], dict[str, Any]]] = []
    process = FakeProcess()

    def popen(command: list[str], **options: Any) -> FakeProcess:
        launched.append((command, options))
        return process

    monkeypatch.setattr("quapylab.execution.backends.subprocess.Popen", popen)
    monkeypatch.setattr("quapylab.execution.backends.Thread", DormantThread)
    backend = ProcessRunBackend(
        f"sqlite+pysqlite:///{tmp_path / 'runs.sqlite3'}", tmp_path / "artifacts"
    )

    backend.submit("run-123")
    backend.submit("run-123")

    assert len(launched) == 1
    command, options = launched[0]
    assert command[-7:] == [
        "worker",
        "--database-url",
        f"sqlite+pysqlite:///{tmp_path / 'runs.sqlite3'}",
        "--artifact-root",
        str(tmp_path / "artifacts"),
        "--run-id",
        "run-123",
    ]
    assert options["stdin"] is subprocess.DEVNULL
    assert options["stdout"] is subprocess.DEVNULL
    assert options["stderr"] is subprocess.DEVNULL

    backend.close()
    assert process.terminated


def test_process_backend_escalates_cancellation_after_the_grace_period(
    tmp_path: Path, monkeypatch: Any
) -> None:
    process = FakeProcess()
    monkeypatch.setattr("quapylab.execution.backends.subprocess.Popen", lambda *_a, **_k: process)
    monkeypatch.setattr("quapylab.execution.backends.Thread", CancellationThread)
    backend = ProcessRunBackend(
        f"sqlite+pysqlite:///{tmp_path / 'cancel.sqlite3'}",
        tmp_path / "artifacts",
        cancellation_grace_seconds=0.01,
    )

    backend.submit("slow-run")
    backend.cancel("slow-run")

    assert process.terminated


def test_process_backend_executes_a_committed_run_in_a_child_process(tmp_path: Path) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'integration.sqlite3'}"
    artifact_root = tmp_path / "artifacts"
    application = create_app(
        {
            "TESTING": True,
            "QUAPYLAB_DATABASE_URL": database_url,
            "QUAPYLAB_ARTIFACT_ROOT": artifact_root,
        }
    )
    client = application.test_client()
    group_id = str(desktop_actor().group_id)
    project = client.post(
        f"/api/v1/groups/{group_id}/projects", json={"name": "Process experiment"}
    ).get_json()
    workflow = client.post(
        f"/api/v1/projects/{project['id']}/workflows",
        json={
            "name": "Child process",
            "graph": {
                "schema_version": 1,
                "nodes": [{"id": "dataset", "type": "dataset", "version": 1, "config": {}}],
                "edges": [],
            },
        },
    ).get_json()
    run_id = client.post(
        f"/api/v1/projects/{project['id']}/runs",
        json={"workflow_revision_id": workflow["draft_revision_id"], "seed": 3},
    ).get_json()["id"]
    backend = ProcessRunBackend(database_url, artifact_root)

    try:
        backend.submit(run_id)
        deadline = time.monotonic() + 15
        status = "queued"
        environment: dict[str, Any] = {}
        while time.monotonic() < deadline:
            with application.extensions["quapylab.database"].transaction() as session:
                run = session.get(Run, run_id)
                assert run is not None
                status = run.status
                environment = run.environment
            if status not in {"queued", "preparing", "running", "cancelling"}:
                break
            time.sleep(0.05)
    finally:
        backend.close()

    assert status == "failed"
    assert environment["state"]["worker_pid"] != os.getpid()


def test_process_backend_recovers_runs_owned_by_a_previous_instance(tmp_path: Path) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'recovery.sqlite3'}"
    application = create_app({"TESTING": True, "QUAPYLAB_DATABASE_URL": database_url})
    client = application.test_client()
    group_id = str(desktop_actor().group_id)
    project = client.post(
        f"/api/v1/groups/{group_id}/projects", json={"name": "Recovery experiment"}
    ).get_json()
    workflow = client.post(
        f"/api/v1/projects/{project['id']}/workflows",
        json={
            "name": "Interrupted workflow",
            "graph": {
                "schema_version": 1,
                "nodes": [{"id": "dataset", "type": "dataset", "version": 1}],
                "edges": [],
            },
        },
    ).get_json()
    run_id = client.post(
        f"/api/v1/projects/{project['id']}/runs",
        json={"workflow_revision_id": workflow["draft_revision_id"], "seed": 1},
    ).get_json()["id"]
    with application.extensions["quapylab.database"].transaction() as session:
        run = session.get(Run, run_id)
        assert run is not None
        run.status = "running"

    backend = ProcessRunBackend(database_url, tmp_path / "artifacts")

    assert backend.recover() == 1
    assert client.get(f"/api/v1/runs/{run_id}").get_json()["status"] == "interrupted"


def test_process_backend_completes_the_baseline_acc_report_workflow(tmp_path: Path) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'baseline.sqlite3'}"
    artifact_root = tmp_path / "artifacts"
    application = create_app(
        {
            "TESTING": True,
            "QUAPYLAB_DATABASE_URL": database_url,
            "QUAPYLAB_ARTIFACT_ROOT": artifact_root,
        }
    )
    client = application.test_client()
    group_id = str(desktop_actor().group_id)
    project_id = client.post(
        f"/api/v1/groups/{group_id}/projects", json={"name": "Baseline"}
    ).get_json()["id"]
    rows = "\n".join(
        [f"{index},negative" for index in range(12)]
        + [f"{index + 100},positive" for index in range(12)]
    )
    version_id = client.post(
        f"/api/v1/projects/{project_id}/datasets",
        data={
            "name": "Training data",
            "file": (BytesIO(f"feature,label\n{rows}\n".encode()), "training.csv"),
        },
    ).get_json()["id"]
    graph = {
        "schema_version": 1,
        "nodes": [
            {
                "id": "dataset",
                "type": "dataset",
                "version": 1,
                "config": {"dataset_version_id": version_id, "label_column": "label"},
                "position": {"x": 37, "y": 91},
            },
            {"id": "split", "type": "split", "version": 1, "config": {}},
            {"id": "classifier", "type": "classifier", "version": 1, "config": {}},
            {"id": "acc", "type": "acc", "version": 1, "config": {}},
            {"id": "evaluation", "type": "evaluation", "version": 1, "config": {}},
            {"id": "report", "type": "report", "version": 1, "config": {}},
        ],
        "edges": [
            {
                "id": "d-s",
                "source": "dataset",
                "source_port": "dataset",
                "target": "split",
                "target_port": "dataset",
            },
            {
                "id": "s-a",
                "source": "split",
                "source_port": "train",
                "target": "acc",
                "target_port": "train",
            },
            {
                "id": "c-a",
                "source": "classifier",
                "source_port": "classifier",
                "target": "acc",
                "target_port": "classifier",
            },
            {
                "id": "a-e",
                "source": "acc",
                "source_port": "quantifier",
                "target": "evaluation",
                "target_port": "quantifier",
            },
            {
                "id": "s-e",
                "source": "split",
                "source_port": "test",
                "target": "evaluation",
                "target_port": "test",
            },
            {
                "id": "e-r",
                "source": "evaluation",
                "source_port": "report",
                "target": "report",
                "target_port": "evaluation",
            },
        ],
    }
    workflow = client.post(
        f"/api/v1/projects/{project_id}/workflows",
        json={"name": "ACC report", "graph": graph},
    ).get_json()
    revision = client.get(f"/api/v1/workflow-revisions/{workflow['draft_revision_id']}").get_json()
    assert revision["graph"]["nodes"][0]["position"] == {"x": 37.0, "y": 91.0}
    run_id = client.post(
        f"/api/v1/projects/{project_id}/runs",
        json={"workflow_revision_id": workflow["draft_revision_id"], "seed": 7},
    ).get_json()["id"]
    backend = ProcessRunBackend(database_url, artifact_root)
    try:
        backend.submit(run_id)
        deadline = time.monotonic() + 30
        status = "queued"
        while time.monotonic() < deadline:
            status = client.get(f"/api/v1/runs/{run_id}").get_json()["status"]
            if status not in {"queued", "preparing", "running", "cancelling"}:
                break
            time.sleep(0.05)
    finally:
        backend.close()

    result = client.get(f"/api/v1/runs/{run_id}").get_json()
    assert status == "succeeded", result
    nodes = client.get(f"/api/v1/runs/{run_id}/nodes").get_json()["items"]
    report = next(node for node in nodes if node["node_type"] == "report")
    assert report["state"] == "succeeded"
    assert len(report["output_artifact_ids"]) == 1
