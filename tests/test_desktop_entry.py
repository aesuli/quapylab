import sys
from pathlib import Path
from runpy import run_path
from types import SimpleNamespace

from quapylab.app import create_app
from quapylab.desktop import NativeDesktopBridge
from quapylab.domain.actor import desktop_actor


def test_frozen_entrypoint_preserves_worker_subcommand() -> None:
    namespace = run_path(str(Path("packaging") / "desktop_entry.py"))
    calls: list[list[str]] = []

    def fake_main(arguments: list[str]) -> int:
        calls.append(arguments)
        return 17

    namespace["frozen_main"].__globals__["main"] = fake_main

    assert namespace["frozen_main"](["worker", "--run-id", "run-1"]) == 17
    assert namespace["frozen_main"](["--smoke-test"]) == 17
    assert calls == [
        ["worker", "--run-id", "run-1"],
        ["desktop", "--smoke-test"],
    ]


def test_native_bridge_saves_project_export(tmp_path: Path, monkeypatch: object) -> None:
    app = create_app({"TESTING": True})
    client = app.test_client()
    group_id = str(desktop_actor().group_id)
    project = client.post(
        f"/api/v1/groups/{group_id}/projects", json={"name": "Desktop export"}
    ).get_json()
    destination = tmp_path / "project.quapylab.json"

    class Window:
        @staticmethod
        def create_file_dialog(*_: object, **__: object) -> list[str]:
            return [str(destination)]

    monkeypatch.setitem(sys.modules, "webview", SimpleNamespace(SAVE_DIALOG="save"))
    bridge = NativeDesktopBridge(app)
    bridge.bind_window(Window())

    result = bridge.download_export("project", project["id"])

    assert result == {"saved": True, "path": str(destination)}
    assert destination.read_text(encoding="utf-8").startswith("{")
