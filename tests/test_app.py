from quapylab import __version__
from quapylab.app import create_app
from quapylab.config import DeploymentMode
from quapylab.desktop import run_desktop
from quapylab.domain.actor import desktop_actor
from quapylab.persistence.models import Group, GroupMembership, MembershipRole, User


def test_liveness() -> None:
    client = create_app({"TESTING": True}).test_client()

    response = client.get("/api/v1/health/live")

    assert response.status_code == 200
    assert response.get_json() == {"status": "ok"}


def test_version_describes_internal_api() -> None:
    client = create_app({"TESTING": True}).test_client()

    response = client.get("/api/v1/version")

    assert response.status_code == 200
    assert response.get_json() == {
        "api_stability": "internal",
        "application": "quapylab",
        "version": __version__,
    }


def test_frontend_entrypoint_is_available_with_or_without_a_build() -> None:
    client = create_app({"TESTING": True}).test_client()

    response = client.get("/")

    assert response.status_code == 200


def test_unknown_api_route_is_not_rewritten_to_the_frontend() -> None:
    client = create_app({"TESTING": True}).test_client()

    response = client.get("/api/v1/does-not-exist")

    assert response.status_code == 404


def test_desktop_smoke_path_does_not_open_a_window() -> None:
    assert run_desktop(smoke_test=True) == 0


def test_desktop_mode_exposes_a_stable_implicit_actor() -> None:
    application = create_app({"TESTING": True, "QUAPYLAB_MODE": "desktop"})

    with application.test_request_context("/"):
        application.preprocess_request()
        from flask import g

        assert g.actor.deployment_mode == "desktop"


def test_node_types_are_published_by_the_server() -> None:
    client = create_app({"TESTING": True}).test_client()

    response = client.get("/api/v1/node-types")

    assert response.status_code == 200
    body = response.get_json()
    assert body["schema_version"] == 1
    assert [item["type"] for item in body["items"]] == [
        "acc",
        "classifier",
        "dataset",
        "evaluation",
        "split",
    ]


def test_web_mode_is_explicit() -> None:
    application = create_app({"TESTING": True, "QUAPYLAB_MODE": "web"})

    assert application.extensions["quapylab.config"].mode is DeploymentMode.WEB


def test_desktop_session_is_implicit_without_identity_controls() -> None:
    client = create_app({"TESTING": True, "QUAPYLAB_MODE": "desktop"}).test_client()

    response = client.get("/api/v1/auth/session")

    assert response.status_code == 200
    assert response.get_json()["deployment_mode"] == "desktop"
    assert response.get_json()["csrf_token"] is None


def test_desktop_mode_bootstraps_its_implicit_personal_workspace() -> None:
    application = create_app({"TESTING": True, "QUAPYLAB_MODE": "desktop"})
    database = application.extensions["quapylab.database"]
    actor = desktop_actor()

    with database.transaction() as session:
        assert session.get(User, str(actor.user_id)) is not None
        assert session.get(Group, str(actor.group_id)) is not None
        membership = session.query(GroupMembership).one()
        assert membership.role == MembershipRole.OWNER
