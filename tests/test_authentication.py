from quapylab.app import create_app
from quapylab.services.authentication import AuthenticationService


def web_application():
    application = create_app({"TESTING": True, "QUAPYLAB_MODE": "web"})
    with application.extensions["quapylab.database"].transaction() as session:
        AuthenticationService().create_user(
            session,
            login_name="admin",
            display_name="Administrator",
            password="safe-password-123",
            is_system_administrator=True,
        )
    return application


def test_web_api_requires_a_session() -> None:
    client = web_application().test_client()

    response = client.get("/api/v1/node-types")

    assert response.status_code == 401


def test_login_session_csrf_logout_and_password_change() -> None:
    client = web_application().test_client()

    login = client.post(
        "/api/v1/auth/login", json={"login_name": "admin", "password": "safe-password-123"}
    )

    assert login.status_code == 200
    csrf_token = login.get_json()["csrf_token"]
    assert "HttpOnly" in login.headers["Set-Cookie"]
    assert client.get("/api/v1/auth/session").status_code == 200
    assert client.post("/api/v1/auth/logout").status_code == 403
    assert (
        client.post(
            "/api/v1/auth/password",
            headers={"X-CSRF-Token": csrf_token},
            json={
                "current_password": "safe-password-123",
                "new_password": "different-password-456",
            },
        ).status_code
        == 200
    )
    assert (
        client.post("/api/v1/auth/logout", headers={"X-CSRF-Token": csrf_token}).status_code == 200
    )
    assert client.get("/api/v1/auth/session").status_code == 401
    assert (
        client.post(
            "/api/v1/auth/login", json={"login_name": "admin", "password": "different-password-456"}
        ).status_code
        == 200
    )


def test_login_throttles_repeated_invalid_credentials() -> None:
    client = web_application().test_client()

    for _ in range(5):
        assert (
            client.post(
                "/api/v1/auth/login", json={"login_name": "admin", "password": "wrong"}
            ).status_code
            == 401
        )

    assert (
        client.post(
            "/api/v1/auth/login", json={"login_name": "admin", "password": "safe-password-123"}
        ).status_code
        == 429
    )
