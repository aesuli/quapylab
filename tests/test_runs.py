from flask import Flask

from quapylab.app import create_app
from quapylab.domain.actor import desktop_actor
from quapylab.execution.local_worker import LocalWorker, unavailable_node_executor
from quapylab.persistence.models import Run


class RecordingBackend:
    def __init__(self, application: Flask) -> None:
        self.application = application
        self.submitted: list[str] = []
        self.cancelled: list[str] = []

    def submit(self, run_id: str) -> None:
        database = self.application.extensions["quapylab.database"]
        with database.transaction() as session:
            run = session.get(Run, run_id)
            assert run is not None
            assert run.status == "queued"
        self.submitted.append(run_id)

    def cancel(self, run_id: str) -> None:
        self.cancelled.append(run_id)


def graph() -> dict[str, object]:
    return {
        "schema_version": 1,
        "nodes": [
            {"id": "dataset", "type": "dataset", "version": 1, "config": {}},
            {"id": "split", "type": "split", "version": 1, "config": {}},
            {"id": "classifier", "type": "logistic_regression", "version": 1, "config": {}},
            {"id": "acc", "type": "acc", "version": 1, "config": {}},
            {"id": "fit", "type": "fit_quantifier", "version": 1, "config": {}},
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
                "id": "split-fit",
                "source": "split",
                "source_port": "train",
                "target": "fit",
                "target_port": "train",
            },
            {
                "id": "classifier-acc",
                "source": "classifier",
                "source_port": "classifier",
                "target": "acc",
                "target_port": "classifier",
            },
            {
                "id": "acc-fit",
                "source": "acc",
                "source_port": "quantifier",
                "target": "fit",
                "target_port": "quantifier",
            },
            {
                "id": "split-evaluation",
                "source": "split",
                "source_port": "test",
                "target": "evaluation",
                "target_port": "test",
            },
            {
                "id": "acc-evaluation",
                "source": "fit",
                "source_port": "quantifier",
                "target": "evaluation",
                "target_port": "quantifier",
            },
        ],
    }


def queued_run_client():
    application = create_app({"TESTING": True})
    client = application.test_client()
    group_id = str(desktop_actor().group_id)
    project_response = client.post(
        f"/api/v1/groups/{group_id}/projects", json={"name": "Experiment"}
    )
    project = project_response.get_json()
    workflow = client.post(
        f"/api/v1/projects/{project['id']}/workflows", json={"name": "Baseline", "graph": graph()}
    ).get_json()
    response = client.post(
        f"/api/v1/projects/{project['id']}/runs",
        json={"workflow_revision_id": workflow["draft_revision_id"], "seed": 42},
    )
    return application, client, response


def test_run_snapshots_the_project_quapy_environment() -> None:
    application = create_app({"TESTING": True})
    client = application.test_client()
    group_id = str(desktop_actor().group_id)
    project = client.post(
        f"/api/v1/groups/{group_id}/projects", json={"name": "Configured experiment"}
    ).get_json()
    client.patch(
        f"/api/v1/projects/{project['id']}",
        json={
            "name": project["name"],
            "description": "",
            "settings": {"quapy_environment": {"SAMPLE_SIZE": 37}},
        },
    )
    workflow = client.post(
        f"/api/v1/projects/{project['id']}/workflows",
        json={"name": "Baseline", "graph": graph()},
    ).get_json()
    run_id = client.post(
        f"/api/v1/projects/{project['id']}/runs",
        json={"workflow_revision_id": workflow["draft_revision_id"], "seed": 42},
    ).get_json()["id"]

    with application.extensions["quapylab.database"].transaction() as session:
        run = session.get(Run, run_id)
        assert run is not None
        assert run.environment["quapy_environment"] == {"SAMPLE_SIZE": 37}


def test_runs_are_enqueued_with_structured_node_logs_and_can_be_cancelled() -> None:
    _, client, response = queued_run_client()

    assert response.status_code == 202
    run = response.get_json()
    assert run["status"] == "queued"
    nodes = client.get(f"/api/v1/runs/{run['id']}/nodes").get_json()["items"]
    assert [node["state"] for node in nodes] == ["pending"] * 6
    assert set(nodes[0]["logs"][0]) == {"timestamp", "level", "node_id", "event", "message"}

    cancelled = client.post(f"/api/v1/runs/{run['id']}/cancel")

    assert cancelled.status_code == 200
    assert cancelled.get_json()["status"] == "cancelled"
    nodes = client.get(f"/api/v1/runs/{run['id']}/nodes").get_json()["items"]
    assert {node["state"] for node in nodes} == {"cancelled"}
    history = client.get(f"/api/v1/projects/{run['project_id']}/runs")
    assert history.status_code == 200
    assert history.get_json()["items"][0]["id"] == run["id"]
    assert history.get_json()["items"][0]["requested_at"]
    assert history.get_json()["items"][0]["cancelled_at"]


def test_http_dispatches_only_after_the_run_transaction_commits() -> None:
    application = create_app({"TESTING": True})
    backend = RecordingBackend(application)
    application.extensions["quapylab.run_backend"] = backend
    client = application.test_client()
    group_id = str(desktop_actor().group_id)
    project = client.post(
        f"/api/v1/groups/{group_id}/projects", json={"name": "Dispatched experiment"}
    ).get_json()
    workflow = client.post(
        f"/api/v1/projects/{project['id']}/workflows",
        json={"name": "Baseline", "graph": graph()},
    ).get_json()

    response = client.post(
        f"/api/v1/projects/{project['id']}/runs",
        json={"workflow_revision_id": workflow["draft_revision_id"], "seed": 9},
    )
    run_id = response.get_json()["id"]

    assert backend.submitted == [run_id]
    client.post(f"/api/v1/runs/{run_id}/cancel")
    assert backend.cancelled == [run_id]


def test_local_worker_claims_runs_and_recovers_interrupted_work() -> None:
    application, client, response = queued_run_client()
    run_id = response.get_json()["id"]
    worker = LocalWorker(
        application.extensions["quapylab.database"],
        application.extensions["quapylab.runs"],
        lambda _run, _node: None,
    )

    assert worker.run_once() is True
    completed = client.get(f"/api/v1/runs/{run_id}").get_json()
    assert completed["status"] == "succeeded"
    assert isinstance(completed["environment"]["state"]["worker_pid"], int)

    with application.extensions["quapylab.database"].transaction() as session:
        run = session.get(Run, run_id)
        assert run is not None
        run.status = "running"
    assert worker.recover() == 1
    assert client.get(f"/api/v1/runs/{run_id}").get_json()["status"] == "interrupted"


def test_local_worker_can_claim_a_specific_run() -> None:
    application, client, first_response = queued_run_client()
    first_id = first_response.get_json()["id"]
    database = application.extensions["quapylab.database"]
    with database.transaction() as session:
        first = session.get(Run, first_id)
        assert first is not None
        first.status = "queued"
    worker = LocalWorker(
        database, application.extensions["quapylab.runs"], lambda _run, _node: None
    )

    assert worker.run(first_id) is True
    assert client.get(f"/api/v1/runs/{first_id}").get_json()["status"] == "succeeded"
    assert worker.run("missing-run") is False


def test_unconfigured_worker_fails_without_fabricating_node_outputs() -> None:
    application, client, response = queued_run_client()
    worker = LocalWorker(
        application.extensions["quapylab.database"],
        application.extensions["quapylab.runs"],
        unavailable_node_executor,
    )

    assert worker.run_once() is True
    run = client.get(f"/api/v1/runs/{response.get_json()['id']}").get_json()
    nodes = client.get(f"/api/v1/runs/{run['id']}/nodes").get_json()["items"]

    assert run["status"] == "failed"
    assert "No runtime adapter" in run["error_summary"]
    assert {node["state"] for node in nodes} == {"failed", "skipped"}


def test_worker_exit_reconciliation_finishes_an_escalated_cancellation() -> None:
    application, client, response = queued_run_client()
    run_id = response.get_json()["id"]
    database = application.extensions["quapylab.database"]
    service = application.extensions["quapylab.runs"]
    with database.transaction() as session:
        run = session.get(Run, run_id)
        assert run is not None
        run.status = "running"

    assert client.post(f"/api/v1/runs/{run_id}/cancel").get_json()["status"] == "cancelling"
    with database.transaction() as session:
        service.reconcile_worker_exit(session, run_id, -15)

    assert client.get(f"/api/v1/runs/{run_id}").get_json()["status"] == "cancelled"
    nodes = client.get(f"/api/v1/runs/{run_id}/nodes").get_json()["items"]
    assert {node["state"] for node in nodes} == {"cancelled"}
