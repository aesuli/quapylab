from pathlib import Path

from flask import Flask
from flask.testing import FlaskClient

from quapylab.app import create_app
from quapylab.cli import main
from quapylab.persistence.database import Database
from quapylab.services.authentication import AuthenticationService, utc_now

ADMIN_PASSWORD = "safe-password-123"
USER_PASSWORD = "user-password-123"


def web_application() -> Flask:
    application = create_app({"TESTING": True, "QUAPYLAB_MODE": "web"})
    with application.extensions["quapylab.database"].transaction() as session:
        AuthenticationService().create_user(
            session,
            login_name="admin",
            display_name="Administrator",
            password=ADMIN_PASSWORD,
            is_system_administrator=True,
        )
    return application


def sign_in(application: Flask, login_name: str, password: str) -> tuple[FlaskClient, dict]:
    client = application.test_client()
    response = client.post(
        "/api/v1/auth/login", json={"login_name": login_name, "password": password}
    )
    assert response.status_code == 200
    return client, {"X-CSRF-Token": response.get_json()["csrf_token"]}


def create_user(client: FlaskClient, headers: dict, login_name: str = "alice") -> str:
    response = client.post(
        "/api/v1/users",
        headers=headers,
        json={"login_name": login_name, "display_name": "Alice", "password": USER_PASSWORD},
    )
    assert response.status_code == 201
    return response.get_json()["id"]


def test_administrator_creates_lists_and_deletes_users() -> None:
    application = web_application()
    admin, admin_headers = sign_in(application, "admin", ADMIN_PASSWORD)

    user_id = create_user(admin, admin_headers)

    listed = admin.get("/api/v1/users").get_json()["items"]
    assert [item["login_name"] for item in listed] == ["admin", "alice"]
    alice, alice_headers = sign_in(application, "alice", USER_PASSWORD)
    assert alice.get("/api/v1/auth/session").status_code == 200

    assert admin.delete(f"/api/v1/users/{user_id}", headers=admin_headers).status_code == 204

    assert alice.get("/api/v1/auth/session").status_code == 401
    assert (
        application.test_client()
        .post("/api/v1/auth/login", json={"login_name": "alice", "password": USER_PASSWORD})
        .status_code
        == 401
    )
    assert [item["login_name"] for item in admin.get("/api/v1/users").get_json()["items"]] == [
        "admin"
    ]
    create_user(admin, admin_headers)


def test_user_creation_reports_invalid_input() -> None:
    application = web_application()
    admin, headers = sign_in(application, "admin", ADMIN_PASSWORD)
    create_user(admin, headers)

    duplicate = admin.post(
        "/api/v1/users",
        headers=headers,
        json={"login_name": "ALICE", "display_name": "", "password": USER_PASSWORD},
    )
    short = admin.post(
        "/api/v1/users",
        headers=headers,
        json={"login_name": "bob", "display_name": "", "password": "short"},
    )

    assert duplicate.status_code == short.status_code == 400
    assert duplicate.get_json()["error"]["message"] == "Login name is already in use."
    assert "12 characters" in short.get_json()["error"]["message"]


def test_administrator_resets_a_password_and_ends_the_users_sessions() -> None:
    application = web_application()
    admin, admin_headers = sign_in(application, "admin", ADMIN_PASSWORD)
    user_id = create_user(admin, admin_headers)
    alice, _ = sign_in(application, "alice", USER_PASSWORD)

    response = admin.post(
        f"/api/v1/users/{user_id}/password",
        headers=admin_headers,
        json={"new_password": "reset-password-456"},
    )

    assert response.status_code == 200
    assert alice.get("/api/v1/auth/session").status_code == 401
    old = application.test_client().post(
        "/api/v1/auth/login", json={"login_name": "alice", "password": USER_PASSWORD}
    )
    assert old.status_code == 401
    sign_in(application, "alice", "reset-password-456")
    assert admin.get("/api/v1/auth/session").status_code == 200


def test_any_user_changes_their_own_password() -> None:
    application = web_application()
    admin, admin_headers = sign_in(application, "admin", ADMIN_PASSWORD)
    create_user(admin, admin_headers)
    alice, headers = sign_in(application, "alice", USER_PASSWORD)

    wrong = alice.post(
        "/api/v1/auth/password",
        headers=headers,
        json={"current_password": "not-the-password", "new_password": "another-password-789"},
    )
    changed = alice.post(
        "/api/v1/auth/password",
        headers=headers,
        json={"current_password": USER_PASSWORD, "new_password": "another-password-789"},
    )

    assert wrong.status_code == 400
    assert changed.status_code == 200
    sign_in(application, "alice", "another-password-789")


def test_regular_users_cannot_manage_users() -> None:
    application = web_application()
    admin, admin_headers = sign_in(application, "admin", ADMIN_PASSWORD)
    admin_id = admin.get("/api/v1/users").get_json()["items"][0]["id"]
    create_user(admin, admin_headers)
    alice, headers = sign_in(application, "alice", USER_PASSWORD)

    assert alice.get("/api/v1/users").status_code == 404
    assert (
        alice.post(
            "/api/v1/users",
            headers=headers,
            json={"login_name": "eve", "display_name": "", "password": USER_PASSWORD},
        ).status_code
        == 404
    )
    assert (
        alice.post(
            f"/api/v1/users/{admin_id}/password",
            headers=headers,
            json={"new_password": "takeover-password-1"},
        ).status_code
        == 404
    )
    assert alice.delete(f"/api/v1/users/{admin_id}", headers=headers).status_code == 404
    sign_in(application, "admin", ADMIN_PASSWORD)


def test_administrators_cannot_delete_themselves_or_orphan_a_shared_group() -> None:
    application = web_application()
    admin, admin_headers = sign_in(application, "admin", ADMIN_PASSWORD)
    admin_id = admin.get("/api/v1/users").get_json()["items"][0]["id"]
    user_id = create_user(admin, admin_headers)
    alice, alice_headers = sign_in(application, "alice", USER_PASSWORD)
    assert (
        alice.post(
            "/api/v1/groups", headers=alice_headers, json={"name": "Lab", "slug": "lab"}
        ).status_code
        == 201
    )

    own = admin.delete(f"/api/v1/users/{admin_id}", headers=admin_headers)
    orphaning = admin.delete(f"/api/v1/users/{user_id}", headers=admin_headers)

    assert own.status_code == orphaning.status_code == 400
    assert "Lab" in orphaning.get_json()["error"]["message"]
    assert alice.get("/api/v1/auth/session").status_code == 200


def test_cli_set_password_overwrites_an_existing_password(tmp_path: Path, capsys) -> None:
    database_url = f"sqlite+pysqlite:///{(tmp_path / 'cli.sqlite3').as_posix()}"
    base = ["--database-url", database_url, "--login-name", "root"]
    assert (
        main(["bootstrap-admin", *base, "--display-name", "Root", "--password", ADMIN_PASSWORD])
        == 0
    )

    assert (
        main(["bootstrap-admin", *base, "--display-name", "Root", "--password", USER_PASSWORD])
        == 1
    )
    assert "set-password" in capsys.readouterr().out
    assert main(["set-password", *base, "--password", "overwritten-password-1"]) == 0
    assert main(["set-password", *base, "--password", "short"]) == 1
    assert (
        main(["set-password", "--database-url", database_url, "--login-name", "nobody"]
             + ["--password", USER_PASSWORD])
        == 1
    )

    database = Database(database_url)
    service = AuthenticationService()
    with database.transaction() as session:
        service.authenticate(
            session, login_name="root", password="overwritten-password-1", now=utc_now()
        )
