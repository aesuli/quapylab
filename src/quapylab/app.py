"""Flask application factory for web, standalone, and desktop modes."""

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from flask import Flask, abort, g, jsonify

from quapylab import __version__
from quapylab.config import ApplicationConfig, DeploymentMode
from quapylab.domain.actor import desktop_actor
from quapylab.nodes.registry import default_registry
from quapylab.persistence.database import Database
from quapylab.services.graph_validation import GraphValidationError, validate_workflow
from quapylab.services.workspace import bootstrap_desktop_workspace
from quapylab.workflow.schemas import WorkflowSpec


def create_app(config: Mapping[str, Any] | None = None) -> Flask:
    app = Flask(__name__, static_folder="static")
    app.config.from_mapping(
        JSON_SORT_KEYS=False,
        MAX_CONTENT_LENGTH=256 * 1024 * 1024,
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
    if application_config.mode is DeploymentMode.DESKTOP:
        bootstrap_desktop_workspace(database)

    @app.before_request
    def attach_actor() -> None:
        # Authentication is added on this same seam in the web-mode identity slice.
        # Desktop's identity is deliberately implicit and never client supplied.
        if application_config.mode is DeploymentMode.DESKTOP:
            g.actor = desktop_actor()

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

    @app.get("/api/v1/node-types")
    def node_types() -> tuple[Any, int]:
        registry = app.extensions["quapylab.node_registry"]
        return jsonify({"items": registry.public_definitions(), "schema_version": 1}), 200

    @app.post("/api/v1/workflows/validate")
    def validate_workflow_route() -> tuple[Any, int]:
        payload = request_json()
        try:
            workflow = WorkflowSpec.model_validate(payload)
            compilation = validate_workflow(workflow, app.extensions["quapylab.node_registry"])
        except GraphValidationError as error:
            return jsonify({"error": error.as_dict()}), 422
        except ValueError as error:
            return jsonify(
                {
                    "error": {
                        "code": "invalid_request",
                        "message": "Invalid workflow request.",
                        "details": str(error),
                    }
                }
            ), 400
        return jsonify({"valid": True, "topological_order": compilation.topological_order}), 200

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


app = create_app()
