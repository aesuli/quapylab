"""System-administrator user management for authenticated web deployments."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session, aliased

from quapylab.domain.actor import ActorContext
from quapylab.persistence.models import (
    AuditEvent,
    Group,
    GroupMembership,
    GroupType,
    LocalCredential,
    MembershipRole,
    Run,
    User,
    WebSession,
    WorkflowRevision,
)
from quapylab.services.authentication import AuthenticationService
from quapylab.services.authorization import AuthorizationError


class UserService:
    def __init__(self, authentication: AuthenticationService) -> None:
        self._authentication = authentication

    def list_users(self, session: Session, *, actor: ActorContext) -> list[User]:
        self._require_administrator(actor)
        return list(session.scalars(select(User).order_by(User.login_name)))

    def create_user(
        self,
        session: Session,
        *,
        actor: ActorContext,
        login_name: str,
        display_name: str,
        password: str,
        is_system_administrator: bool = False,
    ) -> User:
        self._require_administrator(actor)
        user = self._authentication.create_user(
            session,
            login_name=login_name,
            display_name=display_name,
            password=password,
            is_system_administrator=is_system_administrator,
        )
        self._audit(session, actor, "user.create", {"user_id": user.id, "login_name": user.login_name})
        return user

    def set_password(
        self, session: Session, *, actor: ActorContext, user_id: str, new_password: str
    ) -> None:
        self._require_administrator(actor)
        self._authentication.set_password(session, user_id=user_id, new_password=new_password)
        self._audit(session, actor, "user.password_reset", {"user_id": user_id})

    def delete_user(self, session: Session, *, actor: ActorContext, user_id: str) -> None:
        self._require_administrator(actor)
        user = session.get(User, user_id)
        if user is None:
            raise AuthorizationError("Resource not found.")
        if user.id == str(actor.user_id):
            raise ValueError("You cannot delete your own account.")
        owners = aliased(GroupMembership)
        sole_owned = session.scalars(
            select(Group.name)
            .join(GroupMembership, GroupMembership.group_id == Group.id)
            .where(
                GroupMembership.user_id == user.id,
                GroupMembership.role == MembershipRole.OWNER.value,
                Group.type == GroupType.SHARED,
                Group.deleted_at.is_(None),
                select(func.count())
                .select_from(owners)
                .where(owners.group_id == Group.id, owners.role == MembershipRole.OWNER.value)
                .scalar_subquery()
                == 1,
            )
        ).all()
        if sole_owned:
            raise ValueError(
                "The user is the only owner of: "
                f"{', '.join(sole_owned)}. Transfer ownership or delete the group first."
            )
        personal_group_ids = list(
            session.scalars(
                select(Group.id)
                .join(GroupMembership, GroupMembership.group_id == Group.id)
                .where(GroupMembership.user_id == user.id, Group.type == GroupType.PERSONAL)
            )
        )
        if personal_group_ids:
            session.execute(
                update(Group)
                .where(Group.id.in_(personal_group_ids), Group.deleted_at.is_(None))
                .values(deleted_at=datetime.now(UTC))
            )
        # Keep provenance records but detach them from the removed account.
        session.execute(
            update(GroupMembership)
            .where(GroupMembership.invited_by_user_id == user.id)
            .values(invited_by_user_id=None)
        )
        session.execute(
            update(WorkflowRevision)
            .where(WorkflowRevision.created_by_user_id == user.id)
            .values(created_by_user_id=None)
        )
        session.execute(
            update(Run).where(Run.requested_by_user_id == user.id).values(requested_by_user_id=None)
        )
        session.execute(
            update(AuditEvent)
            .where(AuditEvent.actor_user_id == user.id)
            .values(actor_user_id=None)
        )
        session.execute(delete(WebSession).where(WebSession.user_id == user.id))
        session.execute(delete(GroupMembership).where(GroupMembership.user_id == user.id))
        session.execute(delete(LocalCredential).where(LocalCredential.user_id == user.id))
        login_name = user.login_name
        session.delete(user)
        session.flush()
        self._audit(session, actor, "user.delete", {"user_id": user_id, "login_name": login_name})

    @staticmethod
    def _require_administrator(actor: ActorContext) -> None:
        if actor.deployment_mode != "web" or not actor.is_system_administrator:
            raise AuthorizationError("Resource not found.")

    @staticmethod
    def _audit(session: Session, actor: ActorContext, action: str, details: dict[str, Any]) -> None:
        session.add(
            AuditEvent(
                actor_user_id=str(actor.user_id),
                group_id=None,
                action=action,
                outcome="success",
                details=details,
            )
        )
