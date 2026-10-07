"""Run dispatch backends kept outside HTTP request execution."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from threading import Lock, Thread
from typing import Protocol

from quapylab.nodes.registry import default_registry
from quapylab.persistence.database import Database
from quapylab.services.runs import RunService


class RunBackend(Protocol):
    def submit(self, run_id: str) -> None: ...

    def cancel(self, run_id: str) -> None: ...


class ProcessRunBackend:
    """Launch each submitted run in its own isolated local process."""

    def __init__(
        self,
        database_url: str,
        artifact_root: Path,
        *,
        cancellation_grace_seconds: float = 5.0,
    ) -> None:
        if cancellation_grace_seconds < 0:
            raise ValueError("Cancellation grace period cannot be negative.")
        self._database_url = database_url
        self._artifact_root = artifact_root
        self._grace = cancellation_grace_seconds
        self._database = Database(database_url)
        self._runs = RunService(default_registry())
        self._processes: dict[str, subprocess.Popen[bytes]] = {}
        self._lock = Lock()

    def submit(self, run_id: str) -> None:
        with self._lock:
            current = self._processes.get(run_id)
            if current is not None and current.poll() is None:
                return
            process = subprocess.Popen(
                [
                    *_entrypoint(),
                    "worker",
                    "--database-url",
                    self._database_url,
                    "--artifact-root",
                    str(self._artifact_root),
                    "--run-id",
                    run_id,
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=_creation_flags(),
            )
            self._processes[run_id] = process
        Thread(
            target=self._watch,
            args=(run_id, process),
            name=f"quapylab-run-{run_id}",
            daemon=True,
        ).start()

    def recover(self) -> int:
        """Mark runs owned by a previous backend instance as interrupted."""
        with self._database.transaction() as session:
            return self._runs.recover_interrupted(session)

    def cancel(self, run_id: str) -> None:
        with self._lock:
            process = self._processes.get(run_id)
        if process is None or process.poll() is not None:
            return
        Thread(
            target=self._terminate_after_grace,
            args=(process,),
            name=f"quapylab-cancel-{run_id}",
            daemon=True,
        ).start()

    def close(self) -> None:
        """Stop child processes when the owning desktop application exits."""
        with self._lock:
            processes = list(self._processes.values())
        for process in processes:
            if process.poll() is None:
                process.terminate()

    def _terminate_after_grace(self, process: subprocess.Popen[bytes]) -> None:
        try:
            process.wait(timeout=self._grace)
        except subprocess.TimeoutExpired:
            process.terminate()

    def _watch(self, run_id: str, process: subprocess.Popen[bytes]) -> None:
        return_code = process.wait()
        with self._lock:
            if self._processes.get(run_id) is process:
                self._processes.pop(run_id, None)
        with self._database.transaction() as session:
            self._runs.reconcile_worker_exit(session, run_id, return_code)


def _entrypoint() -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable]
    return [sys.executable, "-m", "quapylab"]


def _creation_flags() -> int:
    return int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
