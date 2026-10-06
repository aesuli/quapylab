from uuid import UUID

import pytest
from sqlalchemy import select

from quapylab.domain.actor import ActorContext
from quapylab.persistence.database import Database
from quapylab.persistence.models import (
    AuditEvent,
    Group,
    GroupMembership,
    GroupType,
    MembershipRole,
    Project,
)
from quapylab.services.groups import GroupService, MembershipInvariantError


def actor(user_id: str, group_id: str) -> ActorContext:
    return ActorContext(UUID(user_id), UUID(group_id), "web")


def shared_workspace(database: Database) -> tuple[str, str, str, str, str]:
    with database.transaction() as session:
        from quapylab.services.authentication import AuthenticationService

        auth = AuthenticationService()
        owner = auth.create_user(
            session, login_name="owner", display_name="Owner", password="owner-password-123"
        )
        administrator = auth.create_user(
            session, login_name="admin", display_name="Admin", password="admin-password-123"
        )
        editor = auth.create_user(
            session, login_name="editor", display_name="Editor", password="editor-password-123"
        )
        group = Group(name="Research", slug="research", type=GroupType.SHARED)
        session.add(group)
        session.flush()
        session.add_all(
            (
                GroupMembership(group_id=group.id, user_id=owner.id, role=MembershipRole.OWNER),
                GroupMembership(
                    group_id=group.id, user_id=administrator.id, role=MembershipRole.ADMINISTRATOR
                ),
            )
        )
        return (
            owner.id,
            administrator.id,
            editor.id,
            group.id,
            auth.create_user(
                session,
                login_name="destination",
                display_name="Destination",
                password="destination-password-123",
            ).id,
        )


def test_group_roles_owner_invariant_and_audit_log() -> None:
    database = Database("sqlite+pysqlite:///:memory:")
    database.create_schema()
    owner_id, admin_id, editor_id, group_id, _ = shared_workspace(database)
    service = GroupService()

    with database.transaction() as session:
        service.add_member(
            session,
            actor=actor(owner_id, group_id),
            group_id=group_id,
            user_id=editor_id,
            role=MembershipRole.EDITOR,
        )

    with database.transaction() as session:
        with pytest.raises(MembershipInvariantError):
            service.remove_member(
                session, actor=actor(owner_id, group_id), group_id=group_id, user_id=owner_id
            )
        service.change_role(
            session,
            actor=actor(owner_id, group_id),
            group_id=group_id,
            user_id=admin_id,
            role=MembershipRole.OWNER,
        )
        service.remove_member(
            session, actor=actor(owner_id, group_id), group_id=group_id, user_id=owner_id
        )

    with database.transaction() as session:
        events = list(session.scalars(select(AuditEvent).where(AuditEvent.group_id == group_id)))
        assert [event.action for event in events] == [
            "membership.add",
            "membership.role_change",
            "membership.remove",
        ]
        assert (
            session.scalar(
                select(GroupMembership).where(
                    GroupMembership.group_id == group_id,
                    GroupMembership.user_id == owner_id,
                )
            )
            is None
        )


def test_administrator_cannot_assign_or_remove_an_owner() -> None:
    database = Database("sqlite+pysqlite:///:memory:")
    database.create_schema()
    owner_id, admin_id, editor_id, group_id, _ = shared_workspace(database)
    service = GroupService()

    with database.transaction() as session:
        with pytest.raises(PermissionError):
            service.change_role(
                session,
                actor=actor(admin_id, group_id),
                group_id=group_id,
                user_id=owner_id,
                role=MembershipRole.EDITOR,
            )
        with pytest.raises(PermissionError):
            service.add_member(
                session,
                actor=actor(admin_id, group_id),
                group_id=group_id,
                user_id=editor_id,
                role=MembershipRole.OWNER,
            )


def test_project_transfer_requires_administration_of_both_groups() -> None:
    database = Database("sqlite+pysqlite:///:memory:")
    database.create_schema()
    owner_id, _, _, source_group_id, destination_owner_id = shared_workspace(database)
    service = GroupService()

    with database.transaction() as session:
        destination = Group(name="Destination", slug="destination", type=GroupType.SHARED)
        session.add(destination)
        session.flush()
        session.add(
            GroupMembership(
                group_id=destination.id, user_id=owner_id, role=MembershipRole.ADMINISTRATOR
            )
        )
        project = Project(group_id=source_group_id, name="Transfer me")
        session.add(project)
        session.flush()
        service.transfer_project(
            session,
            actor=actor(owner_id, source_group_id),
            project_id=project.id,
            destination_group_id=destination.id,
        )
        assert project.group_id == destination.id
        assert destination_owner_id != owner_id
