"""Single-process local worker for durable queued runs.

Node runtime adapters are intentionally injected: the worker owns lifecycle and
failure handling while the registry/executor layer owns scientific execution.
"""

from __future__ import annotations

from collections.abc import Callable

from quapylab.persistence.database import Database
from quapylab.persistence.models import NodeRun, Run
from quapylab.services.runs import RunService

NodeExecutor = Callable[[Run, NodeRun], None]


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
        with self._database.transaction() as session:
            run = self._runs.claim_next(session)
            if run is None:
                return False
            run_id = run.id
        with self._database.transaction() as session:
            run = session.get(Run, run_id)
            if run is None or not self._runs.start(session, run):
                return True
            nodes = self._runs.list_nodes_for_worker(session, run.id)
            try:
                for node in nodes:
                    if run.status == "cancelling":
                        self._runs.finish_cancelled(session, run)
                        return True
                    node.state = "running"
                    self._execute_node(run, node)
                    self._runs.succeed_node(session, node)
                self._runs.succeed(session, run)
            except Exception as error:
                self._runs.fail(session, run, str(error) or type(error).__name__)
            return True
