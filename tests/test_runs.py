from quapylab.app import create_app
from quapylab.domain.actor import desktop_actor
from quapylab.execution.local_worker import LocalWorker, unavailable_node_executor
from quapylab.persistence.models import Run


def graph() -> dict[str, object]:
    return {
        "schema_version": 1,
        "nodes": [
            {"id": "dataset", "type": "dataset", "version": 1, "config": {}},
            {"id": "split", "type": "split", "version": 1, "config": {}},
            {"id": "classifier", "type": "classifier", "version": 1, "config": {}},
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
                "id": "classifier-acc",
                "source": "classifier",
                "source_port": "classifier",
                "target": "acc",
                "target_port": "classifier",
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
                "source": "acc",
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


def test_runs_are_enqueued_with_structured_node_logs_and_can_be_cancelled() -> None:
    _, client, response = queued_run_client()

    assert response.status_code == 202
    run = response.get_json()
    assert run["status"] == "queued"
    nodes = client.get(f"/api/v1/runs/{run['id']}/nodes").get_json()["items"]
    assert [node["state"] for node in nodes] == ["pending"] * 5
    assert set(nodes[0]["logs"][0]) == {"timestamp", "level", "node_id", "event", "message"}

    cancelled = client.post(f"/api/v1/runs/{run['id']}/cancel")

    assert cancelled.status_code == 200
    assert cancelled.get_json()["status"] == "cancelled"
    nodes = client.get(f"/api/v1/runs/{run['id']}/nodes").get_json()["items"]
    assert {node["state"] for node in nodes} == {"cancelled"}


def test_local_worker_claims_runs_and_recovers_interrupted_work() -> None:
    application, client, response = queued_run_client()
    run_id = response.get_json()["id"]
    worker = LocalWorker(
        application.extensions["quapylab.database"],
        application.extensions["quapylab.runs"],
        lambda _run, _node: None,
    )

    assert worker.run_once() is True
    assert client.get(f"/api/v1/runs/{run_id}").get_json()["status"] == "succeeded"

    with application.extensions["quapylab.database"].transaction() as session:
        run = session.get(Run, run_id)
        assert run is not None
        run.status = "running"
    assert worker.recover() == 1
    assert client.get(f"/api/v1/runs/{run_id}").get_json()["status"] == "interrupted"


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
