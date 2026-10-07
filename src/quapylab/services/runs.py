"""Durable run and node-run state transitions."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from quapylab.domain.actor import ActorContext
from quapylab.persistence.models import NodeRun, Run, WorkflowRevision
from quapylab.services.authorization import AuthorizationError
from quapylab.services.graph_validation import validate_workflow
from quapylab.services.projects import ProjectService
from quapylab.workflow.schemas import WorkflowSpec

RUN_ACTIVE = frozenset({"queued", "preparing", "running", "cancelling"})


class RunService:
    def __init__(self, registry: Any) -> None:
        self._projects = ProjectService()
        self._registry = registry

    def enqueue(
        self,
        session: Session,
        *,
        actor: ActorContext,
        project_id: str,
        workflow_revision_id: str,
        seed: int,
    ) -> Run:
        project = self._projects._project_for_edit(session, actor, project_id)
        revision = session.get(WorkflowRevision, workflow_revision_id)
        if revision is None:
            raise AuthorizationError("Resource not found.")
        workflow = self._projects.get_workflow(
            session, actor=actor, workflow_id=revision.workflow_id
        )
        if workflow.project_id != project.id:
            raise AuthorizationError("Resource not found.")
        graph = WorkflowSpec.model_validate(revision.graph)
        compilation = validate_workflow(graph, self._registry)
        nodes = {node.id: node for node in graph.nodes}
        input_bindings = {
            node.id: {
                edge.target_port: {"node_id": edge.source, "port": edge.source_port}
                for edge in graph.edges
                if edge.target == node.id
            }
            for node in graph.nodes
        }
        run = Run(
            project_id=project.id,
            workflow_revision_id=revision.id,
            status="queued",
            seed=seed,
            environment={"state": {"heartbeat_at": _timestamp()}},
            requested_by_user_id=str(actor.user_id),
        )
        session.add(run)
        session.flush()
        for node_id in compilation.topological_order:
            node = nodes[node_id]
            session.add(
                NodeRun(
                    run_id=run.id,
                    node_id=node.id,
                    node_type=node.type,
                    node_version=node.version,
                    state="pending",
                    # These private fields turn the immutable graph into an execution plan.
                    # Runtime adapters consume artifact bindings rather than locating nodes by type.
                    resolved_config={
                        **node.config,
                        "__inputs": input_bindings[node.id],
                        "__outputs": [
                            port.name
                            for port in self._registry.get(node.type, node.version).outputs
                        ],
                    },
                    logs=[_log(node.id, "info", "node.pending", "Node is queued.")],
                )
            )
        return run

    def get(self, session: Session, *, actor: ActorContext, run_id: str) -> Run:
        run = session.get(Run, run_id)
        if run is None:
            raise AuthorizationError("Resource not found.")
        self._projects.get_project(session, actor=actor, project_id=run.project_id)
        return run

    def list_nodes(self, session: Session, *, actor: ActorContext, run_id: str) -> list[NodeRun]:
        self.get(session, actor=actor, run_id=run_id)
        return self.list_nodes_for_worker(session, run_id)

    def list_nodes_for_worker(self, session: Session, run_id: str) -> list[NodeRun]:
        """Return nodes in the persisted workflow's deterministic execution order.

        Node-run IDs are UUIDs and therefore cannot express dependency order.  Recompile the
        immutable revision so a worker never runs a downstream node merely because its UUID
        sorts first.
        """
        run = session.get(Run, run_id)
        if run is None:
            return []
        revision = session.get(WorkflowRevision, run.workflow_revision_id)
        if revision is None:
            raise RuntimeError("Run references a missing workflow revision.")
        workflow = WorkflowSpec.model_validate(revision.graph)
        compilation = validate_workflow(workflow, self._registry)
        nodes_by_id = {
            node.node_id: node
            for node in session.scalars(select(NodeRun).where(NodeRun.run_id == run_id))
        }
        return [nodes_by_id[node_id] for node_id in compilation.topological_order]

    def cancel(self, session: Session, *, actor: ActorContext, run_id: str) -> Run:
        run = self.get(session, actor=actor, run_id=run_id)
        self._projects._project_for_edit(session, actor, run.project_id)
        if run.status not in RUN_ACTIVE:
            return run
        run.cancelled_at = datetime.now(UTC)
        if run.status == "queued":
            run.status = "cancelled"
            self._set_pending_nodes(session, run.id, "cancelled", "run.cancelled")
        else:
            run.status = "cancelling"
        self._heartbeat(run)
        return run

    def claim_next(self, session: Session) -> Run | None:
        run = session.scalar(select(Run).where(Run.status == "queued").order_by(Run.requested_at))
        if run is None:
            return None
        run.status = "preparing"
        self._heartbeat(run)
        return run

    def start(self, session: Session, run: Run) -> bool:
        if run.status == "cancelling":
            self.finish_cancelled(session, run)
            return False
        if run.status != "preparing":
            return False
        run.status = "running"
        self._heartbeat(run)
        return True

    def succeed_node(self, session: Session, node: NodeRun) -> None:
        node.state = "succeeded"
        node.logs = [*node.logs, _log(node.node_id, "info", "node.succeeded", "Node completed.")]

    def fail(self, session: Session, run: Run, message: str) -> None:
        run.status = "failed"
        run.error_summary = message
        active_node = session.scalar(
            select(NodeRun).where(NodeRun.run_id == run.id, NodeRun.state == "running")
        )
        if active_node is not None:
            active_node.state = "failed"
            active_node.ended_at = datetime.now(UTC)
            active_node.error = {"message": message}
            active_node.logs = [
                *active_node.logs,
                _log(active_node.node_id, "error", "node.failed", message),
            ]
        self._set_pending_nodes(session, run.id, "skipped", "node.skipped")
        self._heartbeat(run)

    def succeed(self, session: Session, run: Run) -> None:
        run.status = "succeeded"
        self._heartbeat(run)

    def finish_cancelled(self, session: Session, run: Run) -> None:
        run.status = "cancelled"
        self._set_pending_nodes(session, run.id, "cancelled", "run.cancelled")
        self._heartbeat(run)

    def recover_interrupted(self, session: Session) -> int:
        active = list(session.scalars(select(Run).where(Run.status.in_(RUN_ACTIVE - {"queued"}))))
        for run in active:
            run.status = "interrupted"
            run.error_summary = "Worker stopped before the run completed."
            self._set_pending_nodes(session, run.id, "skipped", "run.interrupted")
            self._heartbeat(run)
        return len(active)

    @staticmethod
    def _set_pending_nodes(session: Session, run_id: str, state: str, event: str) -> None:
        nodes = session.scalars(select(NodeRun).where(NodeRun.run_id == run_id))
        for node in nodes:
            if node.state in {"pending", "ready"}:
                node.state = state
                node.logs = [
                    *node.logs,
                    _log(node.node_id, "info", event, "Run did not execute node."),
                ]

    @staticmethod
    def _heartbeat(run: Run) -> None:
        run.environment = {**run.environment, "state": {"heartbeat_at": _timestamp()}}


def _timestamp() -> str:
    return datetime.now(UTC).isoformat()


def _log(node_id: str, level: str, event: str, message: str) -> dict[str, str]:
    return {
        "timestamp": _timestamp(),
        "level": level,
        "node_id": node_id,
        "event": event,
        "message": message,
    }
