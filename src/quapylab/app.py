"""Flask application factory for web, standalone, and future desktop modes."""

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from flask import Flask, abort, jsonify

from quapylab import __version__


def create_app(config: Mapping[str, Any] | None = None) -> Flask:
    app = Flask(__name__, static_folder="static")
    app.config.from_mapping(
        JSON_SORT_KEYS=False,
        MAX_CONTENT_LENGTH=256 * 1024 * 1024,
    )
    if config:
        app.config.from_mapping(config)

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


app = create_app()

