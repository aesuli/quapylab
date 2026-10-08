from pathlib import Path

from quapylab.app import create_app
from quapylab.domain.actor import desktop_actor


def graph(name: str = "dataset") -> dict[str, object]:
    return {
        "schema_version": 1,
        "nodes": [
            {"id": name, "type": "dataset", "version": 1, "config": {}},
            {"id": "acc", "type": "acc", "version": 1, "config": {}},
            {"id": "evaluation", "type": "evaluation", "version": 1, "config": {}},
        ],
        "edges": [
            {
                "id": "dataset-acc",
                "source": name,
                "source_port": "dataset",
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


def test_project_and_immutable_workflow_revision_round_trip(tmp_path: Path) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'quapylab.sqlite3'}"
    app = create_app({"TESTING": True, "QUAPYLAB_DATABASE_URL": database_url})
    client = app.test_client()
    group_id = str(desktop_actor().group_id)

    created = client.post(
        f"/api/v1/groups/{group_id}/projects", json={"name": "Experiment", "description": "First"}
    )
    assert created.status_code == 201
    project_id = created.get_json()["id"]
    assert (
        client.get(f"/api/v1/groups/{group_id}/projects").get_json()["items"][0]["id"] == project_id
    )

    workflow_response = client.post(
        f"/api/v1/projects/{project_id}/workflows", json={"name": "Baseline", "graph": graph()}
    )
    assert workflow_response.status_code == 201
    workflow_id = workflow_response.get_json()["id"]
    initial_revision_id = workflow_response.get_json()["draft_revision_id"]
    assert client.get(f"/api/v1/projects/{project_id}/workflows").get_json()["items"] == [
        workflow_response.get_json()
    ]
    revision_response = client.post(
        f"/api/v1/workflows/{workflow_id}/revisions", json={"graph": graph("dataset-v2")}
    )
    assert revision_response.status_code == 201
    revision_id = revision_response.get_json()["id"]
    assert revision_id != initial_revision_id

    restarted = create_app({"TESTING": True, "QUAPYLAB_DATABASE_URL": database_url}).test_client()
    assert (
        restarted.get(f"/api/v1/workflow-revisions/{initial_revision_id}").get_json()["graph"]
        == graph()
    )
    assert restarted.get(f"/api/v1/workflow-revisions/{revision_id}").get_json()["graph"] == graph(
        "dataset-v2"
    )


def test_project_routes_do_not_cross_group_boundaries() -> None:
    client = create_app({"TESTING": True}).test_client()

    response = client.get("/api/v1/groups/00000000-0000-0000-0000-000000000000/projects")

    assert response.status_code == 404


def test_project_and_workflow_lifecycle_operations_preserve_immutable_history() -> None:
    client = create_app({"TESTING": True}).test_client()
    group_id = str(desktop_actor().group_id)
    project = client.post(
        f"/api/v1/groups/{group_id}/projects", json={"name": "Original"}
    ).get_json()
    updated = client.patch(
        f"/api/v1/projects/{project['id']}",
        json={"name": "Renamed", "description": "Lifecycle test"},
    )
    assert updated.status_code == 200
    assert updated.get_json()["name"] == "Renamed"

    workflow = client.post(
        f"/api/v1/projects/{project['id']}/workflows",
        json={"name": "Baseline", "graph": graph()},
    ).get_json()
    renamed = client.patch(f"/api/v1/workflows/{workflow['id']}", json={"name": "Primary"})
    assert renamed.status_code == 200
    assert renamed.get_json()["name"] == "Primary"
    duplicate = client.post(f"/api/v1/workflows/{workflow['id']}/duplicate", json={"name": "Copy"})
    assert duplicate.status_code == 201
    duplicate_revision = client.get(
        f"/api/v1/workflow-revisions/{duplicate.get_json()['draft_revision_id']}"
    ).get_json()
    assert duplicate_revision["graph"] == graph()

    assert client.delete(f"/api/v1/workflows/{workflow['id']}").status_code == 204
    assert client.get(f"/api/v1/workflows/{workflow['id']}").status_code == 404
    listed = client.get(f"/api/v1/projects/{project['id']}/workflows").get_json()["items"]
    assert [item["name"] for item in listed] == ["Copy"]

    assert client.delete(f"/api/v1/projects/{project['id']}").status_code == 204
    assert client.get(f"/api/v1/projects/{project['id']}").status_code == 404
    assert client.get(f"/api/v1/groups/{group_id}/projects").get_json()["items"] == []
