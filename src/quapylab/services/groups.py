"""Group membership and project-transfer services with auditable authorization."""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from quapylab.domain.actor import ActorContext
from quapylab.persistence.models import (
    AuditEvent,
    Group,
    GroupMembership,
    GroupType,
    MembershipRole,
    Project,
    User,
)
from quapylab.services.authorization import AuthorizationError, Permission, require_group_permission


class MembershipInvariantError(ValueError):
    """Raised when a membership change would leave a group without an owner."""


class GroupService:
    def create_shared_group(
        self, session: Session, *, actor: ActorContext, name: str, slug: str
    ) -> Group:
        normalized_name, normalized_slug = name.strip(), slug.strip().lower()
        if not normalized_name or not normalized_slug:
            raise ValueError("Group name and slug are required.")
        if session.scalar(select(Group).where(Group.slug == normalized_slug)) is not None:
            raise ValueError("Group slug is already in use.")
        group = Group(name=normalized_name, slug=normalized_slug, type=GroupType.SHARED)
        session.add(group)
        session.flush()
        session.add(
            GroupMembership(
                group_id=group.id, user_id=str(actor.user_id), role=MembershipRole.OWNER
            )
        )
        self._audit(session, actor, group.id, "group.create", {"slug": normalized_slug})
        return group

    @staticmethod
    def list_groups(
        session: Session, *, actor: ActorContext
    ) -> list[tuple[Group, GroupMembership]]:
        return list(
            session.execute(
                select(Group, GroupMembership)
                .join(GroupMembership, GroupMembership.group_id == Group.id)
                .where(GroupMembership.user_id == str(actor.user_id))
                .order_by(Group.name)
            )
        )

    def list_members(
        self, session: Session, *, actor: ActorContext, group_id: str
    ) -> list[GroupMembership]:
        self._require(session, actor, group_id, Permission.READ_PROJECT, "membership.list")
        return list(
            session.scalars(
                select(GroupMembership)
                .where(GroupMembership.group_id == group_id)
                .order_by(GroupMembership.created_at, GroupMembership.id)
            )
        )

    def add_member(
        self,
        session: Session,
        *,
        actor: ActorContext,
        group_id: str,
        user_id: str,
        role: MembershipRole,
    ) -> GroupMembership:
        group = self._managed_group(session, group_id)
        self._require(session, actor, group_id, Permission.MANAGE_MEMBERS, "membership.add")
        self._require_owner_for_owner_change(session, actor, group_id, role)
        if session.get(User, user_id) is None:
            raise ValueError("User does not exist.")
        if session.scalar(
            select(GroupMembership).where(
                GroupMembership.group_id == group.id, GroupMembership.user_id == user_id
            )
        ):
            raise ValueError("User is already a group member.")
        membership = GroupMembership(
            group_id=group.id,
            user_id=user_id,
            role=role,
            invited_by_user_id=str(actor.user_id),
        )
        session.add(membership)
        self._audit(
            session, actor, group_id, "membership.add", {"member_user_id": user_id, "role": role}
        )
        return membership

    def change_role(
        self,
        session: Session,
        *,
        actor: ActorContext,
        group_id: str,
        user_id: str,
        role: MembershipRole,
    ) -> GroupMembership:
        self._managed_group(session, group_id)
        self._require(session, actor, group_id, Permission.MANAGE_MEMBERS, "membership.role_change")
        membership = self._membership(session, group_id, user_id)
        self._require_owner_for_owner_change(session, actor, group_id, membership.role)
        self._require_owner_for_owner_change(session, actor, group_id, role)
        if (
            MembershipRole(membership.role) is MembershipRole.OWNER
            and role is not MembershipRole.OWNER
        ):
            self._ensure_not_last_owner(session, group_id)
        membership.role = role
        self._audit(
            session,
            actor,
            group_id,
            "membership.role_change",
            {"member_user_id": user_id, "role": role},
        )
        return membership

    def remove_member(
        self, session: Session, *, actor: ActorContext, group_id: str, user_id: str
    ) -> None:
        self._managed_group(session, group_id)
        self._require(session, actor, group_id, Permission.MANAGE_MEMBERS, "membership.remove")
        membership = self._membership(session, group_id, user_id)
        self._require_owner_for_owner_change(session, actor, group_id, membership.role)
        if MembershipRole(membership.role) is MembershipRole.OWNER:
            self._ensure_not_last_owner(session, group_id)
        session.delete(membership)
        self._audit(session, actor, group_id, "membership.remove", {"member_user_id": user_id})

    def transfer_project(
        self,
        session: Session,
        *,
        actor: ActorContext,
        project_id: str,
        destination_group_id: str,
    ) -> Project:
        project = session.get(Project, project_id)
        if project is None:
            raise AuthorizationError("Resource not found.")
        self._managed_group(session, destination_group_id)
        self._require(session, actor, project.group_id, Permission.MANAGE_GROUP, "project.transfer")
        self._require(
            session, actor, destination_group_id, Permission.MANAGE_GROUP, "project.transfer"
        )
        source_group_id = project.group_id
        project.group_id = destination_group_id
        project.version += 1
        self._audit(
            session,
            actor,
            source_group_id,
            "project.transfer",
            {"project_id": project.id, "destination_group_id": destination_group_id},
        )
        return project

    def _managed_group(self, session: Session, group_id: str) -> Group:
        group = session.get(Group, group_id)
        if group is None:
            raise AuthorizationError("Resource not found.")
        if group.type == GroupType.PERSONAL:
            raise ValueError("Personal workspaces cannot be managed as shared groups.")
        return group

    def _require(
        self,
        session: Session,
        actor: ActorContext,
        group_id: str,
        permission: Permission,
        action: str,
    ) -> None:
        membership = session.scalar(
            select(GroupMembership).where(
                GroupMembership.group_id == group_id,
                GroupMembership.user_id == str(actor.user_id),
            )
        )
        try:
            require_group_permission(
                actor, group_id, MembershipRole(membership.role) if membership else None, permission
            )
        except AuthorizationError:
            self._audit(session, actor, group_id, action, {}, outcome="denied")
            raise

    def _require_owner_for_owner_change(
        self, session: Session, actor: ActorContext, group_id: str, role: str | MembershipRole
    ) -> None:
        if MembershipRole(role) is not MembershipRole.OWNER:
            return
        actor_membership = self._membership(session, group_id, str(actor.user_id))
        if MembershipRole(actor_membership.role) is not MembershipRole.OWNER:
            raise AuthorizationError("Resource not found.")

    @staticmethod
    def _membership(session: Session, group_id: str, user_id: str) -> GroupMembership:
        membership = session.scalar(
            select(GroupMembership).where(
                GroupMembership.group_id == group_id, GroupMembership.user_id == user_id
            )
        )
        if membership is None:
            raise AuthorizationError("Resource not found.")
        return membership

    @staticmethod
    def _ensure_not_last_owner(session: Session, group_id: str) -> None:
        owner_count = session.scalar(
            select(func.count())
            .select_from(GroupMembership)
            .where(
                GroupMembership.group_id == group_id,
                GroupMembership.role == MembershipRole.OWNER.value,
            )
        )
        if owner_count == 1:
            raise MembershipInvariantError("A group must retain at least one owner.")

    @staticmethod
    def _audit(
        session: Session,
        actor: ActorContext,
        group_id: str,
        action: str,
        details: dict[str, Any],
        *,
        outcome: str = "success",
    ) -> None:
        session.add(
            AuditEvent(
                actor_user_id=str(actor.user_id),
                group_id=group_id,
                action=action,
                outcome=outcome,
                details=details,
            )
        )
