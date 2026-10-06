"""Authorized project and immutable workflow-revision persistence."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from quapylab.domain.actor import ActorContext
from quapylab.persistence.models import (
    GroupMembership,
    MembershipRole,
    Project,
    Workflow,
    WorkflowRevision,
)
from quapylab.services.authorization import AuthorizationError, Permission, require_group_permission


class ProjectService:
    def create_project(
        self,
        session: Session,
        *,
        actor: ActorContext,
        group_id: str,
        name: str,
        description: str = "",
    ) -> Project:
        self._require(session, actor, group_id, Permission.EDIT_PROJECT)
        if not name.strip():
            raise ValueError("Project name is required.")
        project = Project(group_id=group_id, name=name.strip(), description=description.strip())
        session.add(project)
        return project

    def list_projects(
        self, session: Session, *, actor: ActorContext, group_id: str
    ) -> list[Project]:
        self._require(session, actor, group_id, Permission.READ_PROJECT)
        return list(
            session.scalars(
                select(Project).where(Project.group_id == group_id).order_by(Project.name)
            )
        )

    def get_project(self, session: Session, *, actor: ActorContext, project_id: str) -> Project:
        project = session.get(Project, project_id)
        if project is None:
            raise AuthorizationError("Resource not found.")
        self._require(session, actor, project.group_id, Permission.READ_PROJECT)
        return project

    def list_workflows(
        self, session: Session, *, actor: ActorContext, project_id: str
    ) -> list[Workflow]:
        project = self.get_project(session, actor=actor, project_id=project_id)
        return list(
            session.scalars(
                select(Workflow).where(Workflow.project_id == project.id).order_by(Workflow.name)
            )
        )

    def create_workflow(
        self,
        session: Session,
        *,
        actor: ActorContext,
        project_id: str,
        name: str,
        graph: dict[str, Any],
    ) -> Workflow:
        project = self._project_for_edit(session, actor, project_id)
        if not name.strip():
            raise ValueError("Workflow name is required.")
        workflow = Workflow(project_id=project.id, name=name.strip())
        session.add(workflow)
        session.flush()
        revision = self._new_revision(session, actor, workflow.id, graph)
        workflow.draft_revision_id = revision.id
        return workflow

    def add_revision(
        self, session: Session, *, actor: ActorContext, workflow_id: str, graph: dict[str, Any]
    ) -> WorkflowRevision:
        workflow = self._workflow_for_edit(session, actor, workflow_id)
        revision = self._new_revision(session, actor, workflow.id, graph)
        workflow.draft_revision_id = revision.id
        workflow.version += 1
        return revision

    def get_workflow(self, session: Session, *, actor: ActorContext, workflow_id: str) -> Workflow:
        workflow = session.get(Workflow, workflow_id)
        if workflow is None:
            raise AuthorizationError("Resource not found.")
        project = self.get_project(session, actor=actor, project_id=workflow.project_id)
        if project.id != workflow.project_id:
            raise AssertionError("Unexpected project lookup mismatch.")
        return workflow

    def get_revision(
        self, session: Session, *, actor: ActorContext, revision_id: str
    ) -> WorkflowRevision:
        revision = session.get(WorkflowRevision, revision_id)
        if revision is None:
            raise AuthorizationError("Resource not found.")
        self.get_workflow(session, actor=actor, workflow_id=revision.workflow_id)
        return revision

    def _project_for_edit(self, session: Session, actor: ActorContext, project_id: str) -> Project:
        project = session.get(Project, project_id)
        if project is None:
            raise AuthorizationError("Resource not found.")
        self._require(session, actor, project.group_id, Permission.EDIT_PROJECT)
        return project

    def _workflow_for_edit(
        self, session: Session, actor: ActorContext, workflow_id: str
    ) -> Workflow:
        workflow = session.get(Workflow, workflow_id)
        if workflow is None:
            raise AuthorizationError("Resource not found.")
        self._project_for_edit(session, actor, workflow.project_id)
        return workflow

    @staticmethod
    def _new_revision(
        session: Session, actor: ActorContext, workflow_id: str, graph: dict[str, Any]
    ) -> WorkflowRevision:
        encoded = json.dumps(
            graph, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode()
        revision = WorkflowRevision(
            workflow_id=workflow_id,
            schema_version=graph.get("schema_version", 1),
            graph=graph,
            content_hash=hashlib.sha256(encoded).hexdigest(),
            created_by_user_id=str(actor.user_id),
        )
        session.add(revision)
        session.flush()
        return revision

    @staticmethod
    def _require(
        session: Session, actor: ActorContext, group_id: str, permission: Permission
    ) -> None:
        membership = session.scalar(
            select(GroupMembership).where(
                GroupMembership.group_id == group_id,
                GroupMembership.user_id == str(actor.user_id),
            )
        )
        require_group_permission(
            actor,
            group_id,
            MembershipRole(membership.role) if membership else None,
            permission,
        )
