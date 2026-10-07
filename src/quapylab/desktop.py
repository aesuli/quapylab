"""Native pywebview host for the same Flask application used on the web."""

from pathlib import Path
from typing import Any, cast

from quapylab.app import create_app
from quapylab.execution.backends import ProcessRunBackend


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
    window = webview.create_window(
        "QuaPy Lab",
        cast(Any, application),
        width=1280,
        height=800,
        min_size=(900, 600),
    )
    window.events.closed += backend.close
    try:
        webview.start()
    finally:
        backend.close()
    return 0

