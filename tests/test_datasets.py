from io import BytesIO

from quapylab.app import create_app
from quapylab.domain.actor import desktop_actor
from quapylab.persistence.models import Group, GroupMembership, GroupType, MembershipRole
from quapylab.services.authentication import AuthenticationService


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
    artifact_id = version["source"]["artifact_id"]
    metadata = client.get(f"/api/v1/artifacts/{artifact_id}")
    assert metadata.status_code == 200
    assert metadata.get_json()["sha256"] == version["artifact_hashes"][0]
    download = client.get(f"/api/v1/artifacts/{artifact_id}/download")
    assert download.status_code == 200
    assert download.data == b"feature,label\n1,yes\n2,no\n"
    assert download.headers["X-Content-Type-Options"] == "nosniff"


def test_identical_uploads_reuse_the_same_content_addressed_artifact() -> None:
    client = create_app({"TESTING": True}).test_client()
    group_id = str(desktop_actor().group_id)
    project = client.post(f"/api/v1/groups/{group_id}/projects", json={"name": "Experiment"})
    project_id = project.get_json()["id"]
    payload = b"feature\n1\n"

    first = client.post(
        f"/api/v1/projects/{project_id}/datasets",
        data={"name": "One", "file": (BytesIO(payload), "one.csv")},
    ).get_json()
    second = client.post(
        f"/api/v1/projects/{project_id}/datasets",
        data={"name": "Two", "file": (BytesIO(payload), "two.csv")},
    ).get_json()

    assert first["artifact_hashes"] == second["artifact_hashes"]
    assert first["source"]["artifact_id"] == second["source"]["artifact_id"]


def test_artifact_download_does_not_disclose_another_groups_content() -> None:
    application = create_app({"TESTING": True, "QUAPYLAB_MODE": "web"})
    with application.extensions["quapylab.database"].transaction() as session:
        authentication = AuthenticationService()
        owner = authentication.create_user(
            session, login_name="owner", display_name="Owner", password="safe-password-123"
        )
        outsider = authentication.create_user(
            session, login_name="outsider", display_name="Outsider", password="safe-password-123"
        )
        owner_group = Group(name="Owner group", slug="owner-group", type=GroupType.SHARED)
        outsider_group = Group(name="Outsider group", slug="outsider-group", type=GroupType.SHARED)
        session.add_all((owner_group, outsider_group))
        session.flush()
        session.add_all(
            (
                GroupMembership(
                    group_id=owner_group.id, user_id=owner.id, role=MembershipRole.OWNER
                ),
                GroupMembership(
                    group_id=outsider_group.id, user_id=outsider.id, role=MembershipRole.OWNER
                ),
            )
        )
        artifact = application.extensions["quapylab.artifacts"].create(
            session,
            group_id=owner_group.id,
            kind="dataset-source",
            media_type="text/csv",
            content=b"secret\n",
            serialization={"format": "csv"},
        )
        artifact_id = artifact.id

    client = application.test_client()
    client.post(
        "/api/v1/auth/login", json={"login_name": "outsider", "password": "safe-password-123"}
    )

    assert client.get(f"/api/v1/artifacts/{artifact_id}").status_code == 404
    assert client.get(f"/api/v1/artifacts/{artifact_id}/download").status_code == 404


def test_quapy_source_catalog_exposes_loader_specific_options() -> None:
    client = create_app({"TESTING": True}).test_client()

    response = client.get("/api/v1/dataset-sources/quapy")

    assert response.status_code == 200
    sources = {item["id"]: item for item in response.get_json()["items"]}
    assert sources["reviews"]["options"] == {"tfidf": "boolean", "min_df": "integer|null"}
    assert sources["twitter"]["options"]["for_model_selection"] == "boolean"
