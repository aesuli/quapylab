from quapylab.app import create_app


def test_api_errors_have_a_request_id_and_echo_valid_request_ids() -> None:
    client = create_app({"TESTING": True}).test_client()

    response = client.get("/api/v1/unknown", headers={"X-Request-ID": "client_request-1"})

    assert response.status_code == 404
    assert response.headers["X-Request-ID"] == "client_request-1"
    assert response.get_json()["error"] == {
        "code": "not_found",
        "message": "Resource not found.",
        "request_id": "client_request-1",
    }


def test_desktop_mode_does_not_expose_group_management() -> None:
    client = create_app({"TESTING": True}).test_client()

    created = client.post("/api/v1/groups", json={"name": "Research", "slug": "research"})

    assert created.status_code == 404
    groups = client.get("/api/v1/groups")
    assert groups.status_code == 200
    assert groups.get_json()["items"][0]["slug"] == "local-personal-workspace"
