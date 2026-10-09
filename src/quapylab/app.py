"""Flask application factory for web, standalone, and desktop modes."""

import csv
import hmac
import io
import json
import re
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from uuid import uuid4

from flask import Flask, Response, abort, g, jsonify, request

from quapylab import __version__
from quapylab.config import ApplicationConfig, DeploymentMode
from quapylab.domain.actor import desktop_actor
from quapylab.nodes.registry import default_registry
from quapylab.persistence.database import Database
from quapylab.persistence.models import MembershipRole, User, WebSession
from quapylab.services.artifacts import ArtifactService, LocalArtifactStore
from quapylab.services.authentication import (
    AuthenticationError,
    AuthenticationService,
    CsrfError,
    LoginThrottle,
    utc_now,
)
from quapylab.services.authorization import AuthorizationError
from quapylab.services.datasets import DatasetService
from quapylab.services.graph_validation import GraphValidationError, validate_workflow
from quapylab.services.groups import GroupService
from quapylab.services.portability import PortabilityService
from quapylab.services.projects import ProjectService
from quapylab.services.runs import RunService
from quapylab.services.workspace import bootstrap_desktop_workspace
from quapylab.workflow.schemas import WorkflowSpec


def create_app(config: Mapping[str, Any] | None = None) -> Flask:
    app = Flask(__name__, static_folder="static")
    app.config.from_mapping(
        JSON_SORT_KEYS=False,
        MAX_CONTENT_LENGTH=256 * 1024 * 1024,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
    )
    if config:
        app.config.from_mapping(config)

    application_config = ApplicationConfig.from_mapping(app.config)
    app.extensions["quapylab.config"] = application_config
    app.extensions["quapylab.node_registry"] = default_registry()
    database_url = (
        "sqlite+pysqlite:///:memory:"
        if app.config.get("TESTING") and "QUAPYLAB_DATABASE_URL" not in app.config
        else application_config.database_url
    )
    database = Database(database_url)
    database.create_schema()
    app.extensions["quapylab.database"] = database
    app.extensions["quapylab.authentication"] = AuthenticationService()
    app.extensions["quapylab.login_throttle"] = LoginThrottle()
    app.extensions["quapylab.projects"] = ProjectService()
    app.extensions["quapylab.groups"] = GroupService()
    app.extensions["quapylab.runs"] = RunService(app.extensions["quapylab.node_registry"])
    artifact_root = app.config.get("QUAPYLAB_ARTIFACT_ROOT")
    if artifact_root is None:
        artifact_root = (
            tempfile.mkdtemp(prefix="quapylab-artifacts-")
            if app.config.get("TESTING")
            else Path(".quapylab") / "artifacts"
        )
    artifacts = ArtifactService(LocalArtifactStore(Path(artifact_root)))
    app.extensions["quapylab.artifact_root"] = Path(artifact_root)
    app.extensions["quapylab.artifacts"] = artifacts
    app.extensions["quapylab.datasets"] = DatasetService(artifacts)
    app.extensions["quapylab.portability"] = PortabilityService(artifacts)
    if not config or "SESSION_COOKIE_SECURE" not in config:
        app.config["SESSION_COOKIE_SECURE"] = not app.config.get("TESTING", False)
    if application_config.mode is DeploymentMode.DESKTOP:
        bootstrap_desktop_workspace(database)

    @app.before_request
    def attach_actor() -> None:
        supplied_request_id = request.headers.get("X-Request-ID", "")
        g.request_id = (
            supplied_request_id
            if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", supplied_request_id)
            else uuid4().hex
        )
        # Desktop's identity is deliberately implicit and never client supplied.
        if application_config.mode is DeploymentMode.DESKTOP:
            g.actor = desktop_actor()
            return
        if request.endpoint in {"health_live", "login"}:
            return
        authentication: AuthenticationService = app.extensions["quapylab.authentication"]
        with database.transaction() as session:
            authenticated = authentication.load_session(
                session, token=request.cookies.get("quapylab_session"), now=utc_now()
            )
            g.actor = authenticated.actor
            g.web_session_id = authenticated.session.id
            g.csrf_token = authenticated.session.csrf_token
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            if not g.csrf_token or not hmac.compare_digest(
                g.csrf_token, request.headers.get("X-CSRF-Token", "")
            ):
                raise CsrfError("CSRF validation failed.")

    @app.after_request
    def add_request_id(response: Response) -> Response:
        response.headers["X-Request-ID"] = g.request_id
        return response

    @app.errorhandler(AuthenticationError)
    def authentication_error(_: AuthenticationError) -> tuple[Any, int]:
        return api_error("authentication_required", "Authentication is required.", 401)

    @app.errorhandler(CsrfError)
    def csrf_error(_: CsrfError) -> tuple[Any, int]:
        return api_error("csrf_failed", "CSRF validation failed.", 403)

    @app.errorhandler(AuthorizationError)
    def authorization_error(_: AuthorizationError) -> tuple[Any, int]:
        return api_error("not_found", "Resource not found.", 404)

    @app.errorhandler(ValueError)
    def invalid_request(error: ValueError) -> tuple[Any, int]:
        return api_error("invalid_request", "Invalid request.", 400, str(error))

    @app.errorhandler(404)
    def route_not_found(_: Any) -> tuple[Any, int]:
        if request.path.startswith("/api/"):
            return api_error("not_found", "Resource not found.", 404)
        return "Not found.", 404

    @app.get("/api/v1/health/live")
    def health_live() -> tuple[Any, int]:
        return jsonify({"status": "ok"}), 200

    @app.get("/api/v1/version")
    def application_version() -> tuple[Any, int]:
        return jsonify(
            {
                "application": "quapylab",
                "version": __version__,
                "api_stability": "internal",
            }
        ), 200

    @app.post("/api/v1/auth/login")
    def login() -> tuple[Any, int]:
        payload = request_json()
        login_name = payload.get("login_name")
        password = payload.get("password")
        if not isinstance(login_name, str) or not isinstance(password, str):
            return api_error("invalid_request", "Invalid login request.", 400)
        client_key = f"{request.remote_addr or 'unknown'}:{login_name.strip().lower()}"
        throttle: LoginThrottle = app.extensions["quapylab.login_throttle"]
        now = utc_now()
        if throttle.is_limited(client_key, now):
            return api_error("login_throttled", "Try again later.", 429)
        authentication: AuthenticationService = app.extensions["quapylab.authentication"]
        try:
            with database.transaction() as session:
                web_session = authentication.authenticate(
                    session, login_name=login_name, password=password, now=now
                )
                csrf_token = web_session.csrf_token
        except AuthenticationError:
            throttle.record_failure(client_key, now)
            return api_error("invalid_credentials", "Invalid login credentials.", 401)
        throttle.reset(client_key)
        response = jsonify({"authenticated": True, "csrf_token": csrf_token})
        response.set_cookie(
            "quapylab_session",
            web_session.id,
            httponly=True,
            secure=app.config["SESSION_COOKIE_SECURE"],
            samesite=app.config["SESSION_COOKIE_SAMESITE"],
            path="/",
        )
        return response, 200

    @app.get("/api/v1/auth/session")
    def current_session() -> tuple[Any, int]:
        actor = g.actor
        if actor.deployment_mode == "desktop":
            return jsonify(
                {
                    "authenticated": True,
                    "deployment_mode": "desktop",
                    "user_id": str(actor.user_id),
                    "active_group_id": str(actor.group_id),
                    "is_system_administrator": False,
                    "csrf_token": None,
                }
            ), 200
        return jsonify(
            {
                "authenticated": True,
                "deployment_mode": "web",
                "user_id": str(actor.user_id),
                "active_group_id": str(actor.group_id),
                "is_system_administrator": actor.is_system_administrator,
                "csrf_token": g.csrf_token,
            }
        ), 200

    @app.post("/api/v1/auth/logout")
    def logout() -> tuple[Any, int]:
        authentication: AuthenticationService = app.extensions["quapylab.authentication"]
        with database.transaction() as session:
            web_session = session.get(WebSession, g.web_session_id)
            if web_session is not None:
                authentication.revoke(web_session, utc_now())
        response = jsonify({"authenticated": False})
        response.delete_cookie("quapylab_session", path="/")
        return response, 200

    @app.post("/api/v1/auth/password")
    def change_password() -> tuple[Any, int]:
        payload = request_json()
        current_password = payload.get("current_password")
        new_password = payload.get("new_password")
        if not isinstance(current_password, str) or not isinstance(new_password, str):
            return api_error("invalid_request", "Invalid password request.", 400)
        authentication: AuthenticationService = app.extensions["quapylab.authentication"]
        try:
            with database.transaction() as session:
                authentication.change_password(
                    session,
                    user_id=str(g.actor.user_id),
                    current_password=current_password,
                    new_password=new_password,
                )
        except (AuthenticationError, ValueError) as error:
            return api_error("password_change_failed", str(error), 400)
        return jsonify({"changed": True}), 200

    @app.get("/api/v1/node-types")
    def node_types() -> tuple[Any, int]:
        registry = app.extensions["quapylab.node_registry"]
        return jsonify({"items": registry.public_definitions(), "schema_version": 1}), 200

    @app.route("/api/v1/groups", methods=["GET", "POST"])
    def groups() -> tuple[Any, int]:
        if request.method != "GET" and g.actor.deployment_mode == "desktop":
            raise AuthorizationError("Resource not found.")
        service: GroupService = app.extensions["quapylab.groups"]
        with database.transaction() as session:
            if request.method == "GET":
                return jsonify(
                    {
                        "items": [
                            group_json(*item)
                            for item in service.list_groups(session, actor=g.actor)
                        ]
                    }
                ), 200
            payload = request_json()
            name, slug = payload.get("name"), payload.get("slug")
            if not isinstance(name, str) or not isinstance(slug, str):
                raise ValueError("Group name and slug are required.")
            group = service.create_shared_group(session, actor=g.actor, name=name, slug=slug)
            session.flush()
            return jsonify(group_json(group, None)), 201

    @app.route("/api/v1/groups/<group_id>", methods=["PATCH", "DELETE"])
    def group(group_id: str) -> tuple[Any, int]:
        if g.actor.deployment_mode == "desktop":
            raise AuthorizationError("Resource not found.")
        service: GroupService = app.extensions["quapylab.groups"]
        with database.transaction() as session:
            if request.method == "DELETE":
                service.delete_group(session, actor=g.actor, group_id=group_id)
                return "", 204
            name = request_json().get("name")
            if not isinstance(name, str):
                raise ValueError("Group name is required.")
            item = service.update_group(session, actor=g.actor, group_id=group_id, name=name)
            return jsonify(group_json(item, None)), 200

    @app.route("/api/v1/groups/<group_id>/members", methods=["GET", "POST"])
    def group_members(group_id: str) -> tuple[Any, int]:
        if g.actor.deployment_mode == "desktop":
            raise AuthorizationError("Resource not found.")
        service: GroupService = app.extensions["quapylab.groups"]
        with database.transaction() as session:
            if request.method == "GET":
                return jsonify(
                    {
                        "items": [
                            membership_json(item, session.get(User, item.user_id))
                            for item in service.list_members(
                                session, actor=g.actor, group_id=group_id
                            )
                        ]
                    }
                ), 200
            payload = request_json()
            user_id, role = payload.get("user_id"), payload.get("role")
            if not isinstance(user_id, str) or not isinstance(role, str):
                raise ValueError("Member user ID and role are required.")
            membership = service.add_member(
                session,
                actor=g.actor,
                group_id=group_id,
                user_id=user_id,
                role=MembershipRole(role),
            )
            session.flush()
            return jsonify(membership_json(membership, session.get(User, membership.user_id))), 201

    @app.route("/api/v1/groups/<group_id>/members/<user_id>", methods=["PATCH", "DELETE"])
    def group_member(group_id: str, user_id: str) -> tuple[Any, int]:
        if g.actor.deployment_mode == "desktop":
            raise AuthorizationError("Resource not found.")
        service: GroupService = app.extensions["quapylab.groups"]
        with database.transaction() as session:
            if request.method == "DELETE":
                service.remove_member(session, actor=g.actor, group_id=group_id, user_id=user_id)
                return "", 204
            payload = request_json()
            role = payload.get("role")
            if not isinstance(role, str):
                raise ValueError("Member role is required.")
            membership = service.change_role(
                session,
                actor=g.actor,
                group_id=group_id,
                user_id=user_id,
                role=MembershipRole(role),
            )
            return jsonify(membership_json(membership, session.get(User, membership.user_id))), 200

    @app.post("/api/v1/workflows/validate")
    def validate_workflow_route() -> tuple[Any, int]:
        payload = request_json()
        try:
            workflow = WorkflowSpec.model_validate(payload)
            compilation = validate_workflow(workflow, app.extensions["quapylab.node_registry"])
        except GraphValidationError as error:
            return api_error(error.code, error.message, 422, error.details)
        except ValueError as error:
            return api_error("invalid_request", "Invalid workflow request.", 400, str(error))
        return jsonify({"valid": True, "topological_order": compilation.topological_order}), 200

    @app.route("/api/v1/groups/<group_id>/projects", methods=["GET", "POST"])
    def projects(group_id: str) -> tuple[Any, int]:
        service: ProjectService = app.extensions["quapylab.projects"]
        with database.transaction() as session:
            if request.method == "GET":
                items = service.list_projects(session, actor=g.actor, group_id=group_id)
                return jsonify({"items": [project_json(item) for item in items]}), 200
            payload = request_json()
            name = payload.get("name")
            description = payload.get("description", "")
            if not isinstance(name, str) or not isinstance(description, str):
                raise ValueError("Project name and description must be strings.")
            project = service.create_project(
                session, actor=g.actor, group_id=group_id, name=name, description=description
            )
            session.flush()
            return jsonify(project_json(project)), 201

    @app.route("/api/v1/projects/<project_id>", methods=["GET", "PATCH", "DELETE"])
    def project(project_id: str) -> tuple[Any, int]:
        service: ProjectService = app.extensions["quapylab.projects"]
        with database.transaction() as session:
            if request.method == "DELETE":
                service.delete_project(session, actor=g.actor, project_id=project_id)
                return "", 204
            if request.method == "PATCH":
                payload = request_json()
                name, description = payload.get("name"), payload.get("description", "")
                settings = payload.get("settings")
                if (
                    not isinstance(name, str)
                    or not isinstance(description, str)
                    or (settings is not None and not isinstance(settings, dict))
                ):
                    raise ValueError("Project name, description, or settings are invalid.")
                item = service.update_project(
                    session,
                    actor=g.actor,
                    project_id=project_id,
                    name=name,
                    description=description,
                    settings=settings,
                )
                return jsonify(project_json(item)), 200
            item = service.get_project(session, actor=g.actor, project_id=project_id)
            return jsonify(project_json(item)), 200

    @app.route("/api/v1/projects/<project_id>/datasets", methods=["GET", "POST"])
    def datasets(project_id: str) -> tuple[Any, int]:
        service: DatasetService = app.extensions["quapylab.datasets"]
        with database.transaction() as session:
            if request.method == "GET":
                return jsonify(
                    {
                        "items": [
                            dataset_json(item)
                            for item in service.list_datasets(
                                session, actor=g.actor, project_id=project_id
                            )
                        ]
                    }
                ), 200
            upload = request.files.get("file")
            name = request.form.get("name", "")
            label_column = request.form.get("label_column") or None
            if upload is None:
                raise ValueError("A CSV or TSV file is required.")
            filename = upload.filename
            if not filename or not filename.lower().endswith((".csv", ".tsv")):
                raise ValueError("Dataset files must be CSV or TSV.")
            version = service.ingest(
                session,
                actor=g.actor,
                project_id=project_id,
                name=name,
                filename=filename,
                content=upload.read(),
                label_column=label_column,
            )
            return jsonify(dataset_version_json(version)), 201

    @app.get("/api/v1/dataset-versions/<version_id>")
    def dataset_version(version_id: str) -> tuple[Any, int]:
        service: DatasetService = app.extensions["quapylab.datasets"]
        with database.transaction() as session:
            return jsonify(
                dataset_version_json(
                    service.get_version(session, actor=g.actor, version_id=version_id)
                )
            ), 200

    @app.delete("/api/v1/datasets/<dataset_id>")
    def delete_dataset(dataset_id: str) -> tuple[Any, int]:
        service: DatasetService = app.extensions["quapylab.datasets"]
        with database.transaction() as session:
            service.delete(session, actor=g.actor, dataset_id=dataset_id)
        return "", 204

    @app.get("/api/v1/artifacts/<artifact_id>")
    def artifact(artifact_id: str) -> tuple[Any, int]:
        service: ArtifactService = app.extensions["quapylab.artifacts"]
        with database.transaction() as session:
            item = service.get(session, actor=g.actor, artifact_id=artifact_id)
            return jsonify(artifact_json(item)), 200

    @app.get("/api/v1/artifacts/<artifact_id>/download")
    def download_artifact(artifact_id: str) -> Response:
        service: ArtifactService = app.extensions["quapylab.artifacts"]
        with database.transaction() as session:
            item, content = service.read(session, actor=g.actor, artifact_id=artifact_id)
        response = Response(content, mimetype=item.media_type)
        response.headers["Content-Disposition"] = (
            f'attachment; filename="{_artifact_filename(item)}"'
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.get("/api/v1/artifacts/<artifact_id>/evaluation.csv")
    def export_evaluation_csv(artifact_id: str) -> Response:
        service: ArtifactService = app.extensions["quapylab.artifacts"]
        with database.transaction() as session:
            item, content = service.read(session, actor=g.actor, artifact_id=artifact_id)
        if item.kind == "evaluation-comparison" and item.media_type == (
            "application/vnd.quapylab.evaluation-comparison+json"
        ):
            return _comparison_csv_response(item, json.loads(content))
        if item.kind != "evaluation-result" or item.media_type != (
            "application/vnd.quapylab.evaluation-result+json"
        ):
            raise ValueError("Artifact is not an evaluation or comparison result.")
        evaluation = json.loads(content)
        output = io.StringIO(newline="")
        writer = csv.writer(output, lineterminator="\r\n")
        classes = [_csv_safe(value) for value in evaluation.get("classes", [])]
        samples = evaluation.get("samples")
        if not isinstance(samples, list):
            samples = [
                {
                    "sample_id": "sample-000000",
                    "true_prevalence": evaluation.get("true_prevalence", []),
                    "estimated_prevalence": evaluation.get("estimated_prevalence", []),
                    "metrics": evaluation.get("metrics", {"ae": evaluation.get("value")}),
                }
            ]
        metric_names = sorted(
            {
                name
                for sample in samples
                if isinstance(sample, dict) and isinstance(sample.get("metrics"), dict)
                for name in sample["metrics"]
            }
        )
        writer.writerow(
            ["sample_id"]
            + [f"true_{value}" for value in classes]
            + [f"estimated_{value}" for value in classes]
            + metric_names
        )
        for sample in samples:
            if not isinstance(sample, dict):
                continue
            writer.writerow(
                [_csv_safe(sample.get("sample_id", ""))]
                + sample.get("true_prevalence", [])
                + sample.get("estimated_prevalence", [])
                + [sample.get("metrics", {}).get(name, "") for name in metric_names]
            )
        response = Response(output.getvalue(), mimetype="text/csv")
        response.headers["Content-Disposition"] = (
            f'attachment; filename="evaluation-{item.sha256[:12]}.csv"'
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.get("/api/v1/dataset-sources/quapy")
    def quapy_dataset_sources() -> tuple[Any, int]:
        return jsonify({"items": DatasetService.quapy_sources()}), 200

    @app.post("/api/v1/projects/<project_id>/datasets/quapy")
    def ingest_quapy_dataset(project_id: str) -> tuple[Any, int]:
        payload = request_json()
        source_id, dataset_name = payload.get("source_id"), payload.get("dataset_name")
        options = payload.get("options", {})
        if (
            not isinstance(source_id, str)
            or not isinstance(dataset_name, str)
            or not isinstance(options, dict)
        ):
            raise ValueError("QuaPy source, dataset name, and options are required.")
        service: DatasetService = app.extensions["quapylab.datasets"]
        with database.transaction() as session:
            version = service.ingest_quapy(
                session,
                actor=g.actor,
                project_id=project_id,
                source_id=source_id,
                dataset_name=dataset_name,
                options=options,
            )
            return jsonify(dataset_version_json(version)), 201

    @app.post("/api/v1/projects/<project_id>/transfer")
    def transfer_project(project_id: str) -> tuple[Any, int]:
        payload = request_json()
        destination_group_id = payload.get("destination_group_id")
        if not isinstance(destination_group_id, str):
            raise ValueError("Destination group ID is required.")
        service: GroupService = app.extensions["quapylab.groups"]
        with database.transaction() as session:
            project = service.transfer_project(
                session,
                actor=g.actor,
                project_id=project_id,
                destination_group_id=destination_group_id,
            )
            return jsonify(project_json(project)), 200

    @app.route("/api/v1/projects/<project_id>/runs", methods=["GET", "POST"])
    def project_runs(project_id: str) -> tuple[Any, int]:
        service: RunService = app.extensions["quapylab.runs"]
        if request.method == "GET":
            with database.transaction() as session:
                runs = service.list_for_project(session, actor=g.actor, project_id=project_id)
                return jsonify({"items": [run_json(item) for item in runs]}), 200
        payload = request_json()
        revision_id = payload.get("workflow_revision_id")
        seed = payload.get("seed", 0)
        if not isinstance(revision_id, str) or not isinstance(seed, int) or isinstance(seed, bool):
            raise ValueError("Workflow revision ID and integer seed are required.")
        with database.transaction() as session:
            run = service.enqueue(
                session,
                actor=g.actor,
                project_id=project_id,
                workflow_revision_id=revision_id,
                seed=seed,
            )
            run_id = run.id
            response = run_json(run)
        backend = app.extensions.get("quapylab.run_backend")
        if backend is not None:
            backend.submit(run_id)
        return jsonify(response), 202

    @app.get("/api/v1/runs/<run_id>")
    def run(run_id: str) -> tuple[Any, int]:
        service: RunService = app.extensions["quapylab.runs"]
        with database.transaction() as session:
            return jsonify(run_json(service.get(session, actor=g.actor, run_id=run_id))), 200

    @app.post("/api/v1/runs/<run_id>/cancel")
    def cancel_run(run_id: str) -> tuple[Any, int]:
        service: RunService = app.extensions["quapylab.runs"]
        with database.transaction() as session:
            response = run_json(service.cancel(session, actor=g.actor, run_id=run_id))
        backend = app.extensions.get("quapylab.run_backend")
        if backend is not None:
            backend.cancel(run_id)
        return jsonify(response), 200

    @app.get("/api/v1/runs/<run_id>/nodes")
    def run_nodes(run_id: str) -> tuple[Any, int]:
        service: RunService = app.extensions["quapylab.runs"]
        with database.transaction() as session:
            nodes = service.list_nodes(session, actor=g.actor, run_id=run_id)
            return jsonify({"items": [node_run_json(item) for item in nodes]}), 200

    @app.post("/api/v1/projects/<project_id>/workflows")
    def create_workflow(project_id: str) -> tuple[Any, int]:
        payload = request_json()
        name, graph = payload.get("name"), payload.get("graph")
        if not isinstance(name, str) or not isinstance(graph, dict):
            raise ValueError("Workflow name and graph are required.")
        workflow = WorkflowSpec.model_validate(graph)
        service: ProjectService = app.extensions["quapylab.projects"]
        with database.transaction() as session:
            item = service.create_workflow(
                session,
                actor=g.actor,
                project_id=project_id,
                name=name,
                graph=workflow.model_dump(mode="json", exclude_none=True),
            )
            session.flush()
            return jsonify(workflow_json(item)), 201

    @app.get("/api/v1/projects/<project_id>/workflows")
    def project_workflows(project_id: str) -> tuple[Any, int]:
        service: ProjectService = app.extensions["quapylab.projects"]
        with database.transaction() as session:
            return jsonify(
                {
                    "items": [
                        workflow_json(item)
                        for item in service.list_workflows(
                            session, actor=g.actor, project_id=project_id
                        )
                    ]
                }
            ), 200

    @app.get("/api/v1/projects/<project_id>/export")
    def export_project(project_id: str) -> Response:
        service: PortabilityService = app.extensions["quapylab.portability"]
        with database.transaction() as session:
            document = service.export_project(session, actor=g.actor, project_id=project_id)
        return _json_download(
            document, f"{_download_name(document['project']['name'])}.quapylab.json"
        )

    @app.post("/api/v1/groups/<group_id>/projects/import")
    def import_project(group_id: str) -> tuple[Any, int]:
        service: PortabilityService = app.extensions["quapylab.portability"]
        with database.transaction() as session:
            item = service.import_project(
                session, actor=g.actor, group_id=group_id, document=request_json()
            )
            session.flush()
            return jsonify(project_json(item)), 201

    @app.get("/api/v1/workflows/<workflow_id>/export")
    def export_workflow(workflow_id: str) -> Response:
        service: PortabilityService = app.extensions["quapylab.portability"]
        with database.transaction() as session:
            document = service.export_workflow(session, actor=g.actor, workflow_id=workflow_id)
        return _json_download(
            document, f"{_download_name(document['workflow']['name'])}.workflow.json"
        )

    @app.post("/api/v1/projects/<project_id>/workflows/import")
    def import_workflow(project_id: str) -> tuple[Any, int]:
        service: PortabilityService = app.extensions["quapylab.portability"]
        with database.transaction() as session:
            item = service.import_workflow(
                session, actor=g.actor, project_id=project_id, document=request_json()
            )
            session.flush()
            return jsonify(workflow_json(item)), 201

    @app.get("/api/v1/workflows/<workflow_id>/export/python")
    def export_workflow_python(workflow_id: str) -> Response:
        include_datasets = request.args.get("include_datasets", "false").lower() in {
            "1",
            "true",
            "yes",
        }
        service: PortabilityService = app.extensions["quapylab.portability"]
        with database.transaction() as session:
            content, filename, media_type = service.python_export(
                session, actor=g.actor, workflow_id=workflow_id, include_datasets=include_datasets
            )
        response = Response(content, mimetype=media_type)
        response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.route("/api/v1/workflows/<workflow_id>", methods=["GET", "PATCH", "DELETE"])
    def workflow(workflow_id: str) -> tuple[Any, int]:
        service: ProjectService = app.extensions["quapylab.projects"]
        with database.transaction() as session:
            if request.method == "DELETE":
                service.delete_workflow(session, actor=g.actor, workflow_id=workflow_id)
                return "", 204
            if request.method == "PATCH":
                name = request_json().get("name")
                if not isinstance(name, str):
                    raise ValueError("Workflow name is required.")
                item = service.update_workflow(
                    session, actor=g.actor, workflow_id=workflow_id, name=name
                )
                return jsonify(workflow_json(item)), 200
            item = service.get_workflow(session, actor=g.actor, workflow_id=workflow_id)
            return jsonify(workflow_json(item)), 200

    @app.post("/api/v1/workflows/<workflow_id>/duplicate")
    def duplicate_workflow(workflow_id: str) -> tuple[Any, int]:
        name = request_json().get("name")
        if not isinstance(name, str):
            raise ValueError("Workflow name is required.")
        service: ProjectService = app.extensions["quapylab.projects"]
        with database.transaction() as session:
            item = service.duplicate_workflow(
                session, actor=g.actor, workflow_id=workflow_id, name=name
            )
            session.flush()
            return jsonify(workflow_json(item)), 201

    @app.post("/api/v1/workflows/<workflow_id>/revisions")
    def add_workflow_revision(workflow_id: str) -> tuple[Any, int]:
        payload = request_json()
        graph = payload.get("graph")
        if not isinstance(graph, dict):
            raise ValueError("Workflow graph is required.")
        workflow = WorkflowSpec.model_validate(graph)
        service: ProjectService = app.extensions["quapylab.projects"]
        with database.transaction() as session:
            revision = service.add_revision(
                session,
                actor=g.actor,
                workflow_id=workflow_id,
                graph=workflow.model_dump(mode="json", exclude_none=True),
            )
            return jsonify(revision_json(revision)), 201

    @app.get("/api/v1/workflow-revisions/<revision_id>")
    def workflow_revision(revision_id: str) -> tuple[Any, int]:
        service: ProjectService = app.extensions["quapylab.projects"]
        with database.transaction() as session:
            return jsonify(
                revision_json(service.get_revision(session, actor=g.actor, revision_id=revision_id))
            ), 200

    @app.get("/")
    def frontend_index() -> Any:
        if not (app.static_folder and (Path(app.static_folder) / "index.html").is_file()):
            return jsonify(
                {
                    "application": "quapylab",
                    "message": "Frontend build not found; run `npm.cmd run build` in frontend.",
                }
            )
        return app.send_static_file("index.html")

    @app.get("/<path:resource>")
    def frontend_resource(resource: str) -> Any:
        if resource.startswith("api/"):
            abort(404)
        if app.static_folder and (Path(app.static_folder) / resource).is_file():
            return app.send_static_file(resource)
        if app.static_folder and (Path(app.static_folder) / "index.html").is_file():
            return app.send_static_file("index.html")
        abort(404)

    return app


def request_json() -> dict[str, Any]:
    from flask import request

    if not request.is_json:
        raise ValueError("Content-Type must be application/json.")
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        raise ValueError("Request body must be a JSON object.")
    return payload


def _csv_safe(value: Any) -> str:
    rendered = str(value)
    return f"'{rendered}" if rendered.startswith(("=", "+", "-", "@")) else rendered


def _download_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip("-.") or "export"


def _json_download(document: dict[str, Any], filename: str) -> Response:
    response = Response(json.dumps(document, indent=2), mimetype="application/json")
    response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


def _comparison_csv_response(artifact: Any, comparison: dict[str, Any]) -> Response:
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\r\n")
    classes = [_csv_safe(value) for value in comparison.get("classes", [])]
    metric_names = comparison.get("metric_names", [])
    writer.writerow(
        ["method_id", "quantifier", "sample_id"]
        + [f"true_{value}" for value in classes]
        + [f"estimated_{value}" for value in classes]
        + metric_names
    )
    for method in comparison.get("methods", []):
        if not isinstance(method, dict):
            continue
        for sample in method.get("samples", []):
            if not isinstance(sample, dict):
                continue
            writer.writerow(
                [
                    _csv_safe(method.get("method_id", "")),
                    _csv_safe(method.get("quantifier", "")),
                    _csv_safe(sample.get("sample_id", "")),
                ]
                + sample.get("true_prevalence", [])
                + sample.get("estimated_prevalence", [])
                + [sample.get("metrics", {}).get(name, "") for name in metric_names]
            )
    response = Response(output.getvalue(), mimetype="text/csv")
    response.headers["Content-Disposition"] = (
        f'attachment; filename="comparison-{artifact.sha256[:12]}.csv"'
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


def _artifact_filename(artifact: Any) -> str:
    extension = {
        "application/json": "json",
        "application/vnd.quapylab.evaluation-result+json": "json",
        "application/vnd.quapylab.evaluation-comparison+json": "json",
        "application/vnd.quapylab.model-selection+json": "json",
        "application/vnd.quapylab.quantifier-spec+json": "json",
        "application/vnd.quapylab.dataset-split+json": "json",
        "application/vnd.quapylab.protocol+json": "json",
        "text/csv": "csv",
        "text/tab-separated-values": "tsv",
        "text/html": "html",
    }.get(artifact.media_type, "bin")
    return f"{artifact.kind}-{artifact.sha256[:12]}.{extension}"


def api_error(code: str, message: str, status: int, details: Any | None = None) -> tuple[Any, int]:
    error: dict[str, Any] = {"code": code, "message": message, "request_id": g.request_id}
    if details is not None:
        error["details"] = details
    return jsonify({"error": error}), status


def group_json(group: Any, membership: Any | None) -> dict[str, Any]:
    item = {"id": group.id, "name": group.name, "slug": group.slug, "type": group.type}
    if membership is not None:
        item["role"] = membership.role
    return item


def membership_json(membership: Any, user: User | None = None) -> dict[str, Any]:
    """Return membership data suitable for both API clients and the group UI."""
    return {
        "user_id": membership.user_id,
        "group_id": membership.group_id,
        "role": membership.role,
        "login_name": user.login_name if user else None,
        "display_name": user.display_name if user else None,
    }


def project_json(project: Any) -> dict[str, Any]:
    return {
        "id": project.id,
        "group_id": project.group_id,
        "name": project.name,
        "description": project.description,
        "settings": project.settings,
        "version": project.version,
    }


def workflow_json(workflow: Any) -> dict[str, Any]:
    return {
        "id": workflow.id,
        "project_id": workflow.project_id,
        "name": workflow.name,
        "draft_revision_id": workflow.draft_revision_id,
        "version": workflow.version,
    }


def revision_json(revision: Any) -> dict[str, Any]:
    return {
        "id": revision.id,
        "workflow_id": revision.workflow_id,
        "schema_version": revision.schema_version,
        "graph": revision.graph,
        "content_hash": revision.content_hash,
    }


def dataset_json(dataset: Any) -> dict[str, Any]:
    return {
        "id": dataset.id,
        "project_id": dataset.project_id,
        "name": dataset.name,
        "is_labelled": dataset.is_labelled,
        "current_version_id": dataset.current_version_id,
    }


def dataset_version_json(version: Any) -> dict[str, Any]:
    return {
        "id": version.id,
        "dataset_id": version.dataset_id,
        "schema": version.schema,
        "row_count": version.row_count,
        "classes": version.classes,
        "source": version.source,
        "ingestion_report": version.ingestion_report,
        "artifact_hashes": version.artifact_hashes,
    }


def artifact_json(artifact: Any) -> dict[str, Any]:
    return {
        "id": artifact.id,
        "kind": artifact.kind,
        "media_type": artifact.media_type,
        "size_bytes": artifact.size_bytes,
        "sha256": artifact.sha256,
        "serialization": artifact.serialization,
        "producing_run_id": artifact.producing_run_id,
        "producing_node_run_id": artifact.producing_node_run_id,
    }


def run_json(run: Any) -> dict[str, Any]:
    return {
        "id": run.id,
        "project_id": run.project_id,
        "workflow_revision_id": run.workflow_revision_id,
        "status": run.status,
        "seed": run.seed,
        "environment": run.environment,
        "error_summary": run.error_summary,
        "requested_at": run.requested_at.isoformat(),
        "cancelled_at": run.cancelled_at.isoformat() if run.cancelled_at else None,
    }


def node_run_json(node: Any) -> dict[str, Any]:
    duration = None
    if node.started_at is not None and node.ended_at is not None:
        duration = (node.ended_at - node.started_at).total_seconds()
    return {
        "id": node.id,
        "node_id": node.node_id,
        "node_type": node.node_type,
        "node_version": node.node_version,
        "state": node.state,
        "resolved_config": node.resolved_config,
        "started_at": node.started_at.isoformat() if node.started_at else None,
        "ended_at": node.ended_at.isoformat() if node.ended_at else None,
        "wall_time_seconds": duration,
        "logs": node.logs,
        "error": node.error,
        "output_artifact_ids": node.output_artifact_ids,
    }


app = create_app()
