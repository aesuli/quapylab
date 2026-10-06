"""Native pywebview host for the same Flask application used on the web."""

from typing import Any, cast

from quapylab.app import create_app


def run_desktop(*, smoke_test: bool = False) -> int:
    application = create_app()
    if smoke_test:
        response = application.test_client().get("/")
        return 0 if response.status_code == 200 else 1

    import webview

    webview.create_window(
        "QuaPy Lab",
        cast(Any, application),
        width=1280,
        height=800,
        min_size=(900, 600),
    )
    webview.start()
    return 0

