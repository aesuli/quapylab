import io
import zipfile

from quapylab.app import create_app
from quapylab.domain.actor import desktop_actor


def test_workflow_json_and_python_exports_round_trip() -> None:
    client = create_app({"TESTING": True}).test_client()
    group_id = str(desktop_actor().group_id)
    project = client.post(
        f"/api/v1/groups/{group_id}/projects", json={"name": "Portable"}
    ).get_json()
    graph = {"schema_version": 1, "nodes": [], "edges": []}
    workflow = client.post(
        f"/api/v1/projects/{project['id']}/workflows",
        json={"name": "Baseline", "graph": graph},
    ).get_json()

    exported = client.get(f"/api/v1/workflows/{workflow['id']}/export")
    assert exported.status_code == 200
    assert exported.get_json()["format"] == "quapylab-portable"
    imported = client.post(
        f"/api/v1/projects/{project['id']}/workflows/import", json=exported.get_json()
    )
    assert imported.status_code == 201
    assert imported.get_json()["name"] == "Baseline"

    script = client.get(f"/api/v1/workflows/{workflow['id']}/export/python")
    assert script.status_code == 200
    assert b"WORKFLOW = json.loads" in script.data
    compile(script.data, "Baseline.py", "exec")
    bundle = client.get(f"/api/v1/workflows/{workflow['id']}/export/python?include_datasets=true")
    with zipfile.ZipFile(io.BytesIO(bundle.data)) as archive:
        assert {"Baseline.py", "workflow.json"} <= set(archive.namelist())


def test_project_export_includes_and_remaps_csv_dataset() -> None:
    client = create_app({"TESTING": True}).test_client()
    group_id = str(desktop_actor().group_id)
    project = client.post(f"/api/v1/groups/{group_id}/projects", json={"name": "Data"}).get_json()
    upload = client.post(
        f"/api/v1/projects/{project['id']}/datasets",
        data={
            "name": "observations",
            "label_column": "label",
            "file": (io.BytesIO(b"x,label\n1,a\n2,b\n"), "data.csv"),
        },
        content_type="multipart/form-data",
    ).get_json()
    graph = {
        "schema_version": 1,
        "nodes": [
            {
                "id": "data",
                "type": "dataset",
                "version": 1,
                "config": {"dataset_version_id": upload["id"]},
            }
        ],
        "edges": [],
    }
    client.post(
        f"/api/v1/projects/{project['id']}/workflows", json={"name": "Uses data", "graph": graph}
    )

    document = client.get(f"/api/v1/projects/{project['id']}/export").get_json()
    assert document["datasets"][0]["content_base64"]
    imported = client.post(f"/api/v1/groups/{group_id}/projects/import", json=document)
    assert imported.status_code == 201
    imported_id = imported.get_json()["id"]
    imported_workflow = client.get(f"/api/v1/projects/{imported_id}/workflows").get_json()["items"][
        0
    ]
    revision = client.get(
        f"/api/v1/workflow-revisions/{imported_workflow['draft_revision_id']}"
    ).get_json()
    assert revision["graph"]["nodes"][0]["config"]["dataset_version_id"] != upload["id"]


def test_rejects_unknown_portable_format() -> None:
    client = create_app({"TESTING": True}).test_client()
    group_id = str(desktop_actor().group_id)
    response = client.post(
        f"/api/v1/groups/{group_id}/projects/import",
        json={"format": "something-else", "format_version": 1, "kind": "project"},
    )
    assert response.status_code == 400
