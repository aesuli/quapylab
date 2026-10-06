from io import BytesIO

from quapylab.app import create_app
from quapylab.domain.actor import desktop_actor


def test_csv_ingestion_exposes_an_immutable_schema_preview() -> None:
    client = create_app({"TESTING": True}).test_client()
    group_id = str(desktop_actor().group_id)
    project = client.post(f"/api/v1/groups/{group_id}/projects", json={"name": "Experiment"})
    response = client.post(
        f"/api/v1/projects/{project.get_json()['id']}/datasets",
        data={
            "name": "Training set",
            "label_column": "label",
            "file": (BytesIO(b"feature,label\n1,yes\n2,no\n"), "training.csv"),
        },
    )

    assert response.status_code == 201
    version = response.get_json()
    assert version["row_count"] == 2
    assert version["schema"]["columns"] == [
        {"name": "feature", "type": "integer", "nullable": False},
        {"name": "label", "type": "string", "nullable": False},
    ]
    assert version["ingestion_report"]["preview"] == [
        {"feature": "1", "label": "yes"},
        {"feature": "2", "label": "no"},
    ]
    assert client.get(f"/api/v1/dataset-versions/{version['id']}").get_json()["id"] == version["id"]


def test_quapy_source_catalog_exposes_loader_specific_options() -> None:
    client = create_app({"TESTING": True}).test_client()

    response = client.get("/api/v1/dataset-sources/quapy")

    assert response.status_code == 200
    sources = {item["id"]: item for item in response.get_json()["items"]}
    assert sources["reviews"]["options"] == {"tfidf": "boolean", "min_df": "integer|null"}
    assert sources["twitter"]["options"]["for_model_selection"] == "boolean"
