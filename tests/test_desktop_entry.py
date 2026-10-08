from pathlib import Path
from runpy import run_path


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
