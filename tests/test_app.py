from quapylab import __version__
from quapylab.app import create_app
from quapylab.desktop import run_desktop


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

