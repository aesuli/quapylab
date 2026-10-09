"""Single-process local worker for durable queued runs.

Node runtime adapters are intentionally injected: the worker owns lifecycle and
failure handling while the registry/executor layer owns scientific execution.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import UTC, datetime
from inspect import signature

from sqlalchemy.orm import Session

from quapylab.persistence.database import Database
from quapylab.persistence.models import NodeRun, Run
from quapylab.services.runs import RunService

NodeExecutor = Callable[..., None]


def unavailable_node_executor(_: Run, __: NodeRun) -> None:
    """Make an unconfigured worker fail safely rather than fabricate results."""
    raise RuntimeError("No runtime adapter is registered for workflow node execution.")


class LocalWorker:
    def __init__(self, database: Database, runs: RunService, execute_node: NodeExecutor) -> None:
        self._database = database
        self._runs = runs
        self._execute_node = execute_node

    def recover(self) -> int:
        with self._database.transaction() as session:
            return self._runs.recover_interrupted(session)

    def run_once(self) -> bool:
        return self.run(None)

    def run(self, run_id: str | None) -> bool:
        with self._database.transaction() as session:
            run = self._runs.claim(session, run_id) if run_id else self._runs.claim_next(session)
            if run is None:
                return False
            self._runs.record_worker_process(session, run.id, os.getpid())
            claimed_run_id = run.id
        with self._database.transaction() as session:
            run = session.get(Run, claimed_run_id)
            if run is None or not self._runs.start(session, run):
                return True

        while True:
            with self._database.transaction() as session:
                run = session.get(Run, claimed_run_id)
                if run is None:
                    return True
                if run.status == "cancelling":
                    self._runs.finish_cancelled(session, run)
                    return True
                node = next(
                    (
                        item
                        for item in self._runs.list_nodes_for_worker(session, run.id)
                        if item.state == "pending"
                    ),
                    None,
                )
                if node is None:
                    if run.status != "cancelling":
                        self._runs.succeed(session, run)
                    return True
                node.state = "running"
                node.started_at = datetime.now(UTC)
                try:
                    self._call_executor(session, run, node)
                    node.ended_at = datetime.now(UTC)
                    self._runs.succeed_node(session, node)
                except Exception as error:
                    self._runs.fail(session, run, str(error) or type(error).__name__)
                    return True

    def _call_executor(self, session: Session, run: Run, node: NodeRun) -> None:
        """Support the original two-argument test/external executor contract."""
        if len(signature(self._execute_node).parameters) >= 3:
            self._execute_node(session, run, node)
        else:
            self._execute_node(run, node)
