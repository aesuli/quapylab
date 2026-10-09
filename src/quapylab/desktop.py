"""Native pywebview host for the same Flask application used on the web."""

import re
from pathlib import Path
from typing import Any, cast

from quapylab.app import create_app
from quapylab.execution.backends import ProcessRunBackend


class NativeDesktopBridge:
    """Small trusted bridge for operations browsers cannot perform reliably."""

    def __init__(self, application: Any) -> None:
        self._application = application
        self._window: Any | None = None

    def bind_window(self, window: Any) -> None:
        self._window = window

    def download_artifact(self, artifact_id: str, csv: bool = False) -> dict[str, Any]:
        if self._window is None or not isinstance(artifact_id, str):
            return {"saved": False, "error": "Native download bridge is unavailable."}
        suffix = "/evaluation.csv" if csv else "/download"
        response = self._application.test_client().get(f"/api/v1/artifacts/{artifact_id}{suffix}")
        return self._save_response(response, f"artifact-{artifact_id}")

    def download_export(
        self, kind: str, resource_id: str, include_datasets: bool = False
    ) -> dict[str, Any]:
        """Save an authorized project/workflow export through the native dialog."""
        if self._window is None or not re.fullmatch(r"[0-9a-fA-F-]{36}", resource_id):
            return {"saved": False, "error": "Native download bridge is unavailable."}
        if kind == "project":
            path = f"/api/v1/projects/{resource_id}/export"
        elif kind == "workflow-json":
            path = f"/api/v1/workflows/{resource_id}/export"
        elif kind == "workflow-python":
            suffix = "?include_datasets=true" if include_datasets else ""
            path = f"/api/v1/workflows/{resource_id}/export/python{suffix}"
        elif kind == "workflow-quapy-python":
            suffix = "?include_datasets=true" if include_datasets else ""
            path = f"/api/v1/workflows/{resource_id}/export/quapy-python{suffix}"
        else:
            return {"saved": False, "error": "Unsupported export type."}
        response = self._application.test_client().get(path)
        return self._save_response(response, f"quapylab-{kind}")

    def _save_response(self, response: Any, fallback_name: str) -> dict[str, Any]:
        import webview

        if response.status_code != 200:
            return {"saved": False, "error": "The export could not be downloaded."}
        disposition = response.headers.get("Content-Disposition", "")
        match = re.search(r'filename="?([^";]+)', disposition, flags=re.IGNORECASE)
        filename = match.group(1) if match else fallback_name
        paths = self._window.create_file_dialog(webview.SAVE_DIALOG, save_filename=filename)
        if not paths:
            return {"saved": False, "cancelled": True}
        destination = Path(paths[0] if isinstance(paths, (list, tuple)) else paths)
        destination.write_bytes(response.data)
        return {"saved": True, "path": str(destination)}


def run_desktop(*, smoke_test: bool = False) -> int:
    application = create_app()
    if smoke_test:
        response = application.test_client().get("/")
        return 0 if response.status_code == 200 else 1

    import webview

    config = application.extensions["quapylab.config"]
    artifact_root = cast(Path, application.extensions["quapylab.artifact_root"])
    backend = ProcessRunBackend(config.database_url, artifact_root)
    backend.recover()
    application.extensions["quapylab.run_backend"] = backend
    bridge = NativeDesktopBridge(application)
    window = webview.create_window(
        "QuaPy Lab",
        cast(Any, application),
        js_api=bridge,
        width=1280,
        height=800,
        min_size=(900, 600),
    )
    bridge.bind_window(window)
    window.events.closed += backend.close
    try:
        webview.start()
    finally:
        backend.close()
    return 0
