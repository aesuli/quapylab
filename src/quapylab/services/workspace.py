"""Bootstrap the passwordless personal workspace used in desktop mode."""

from sqlalchemy import select

from quapylab.domain.actor import desktop_actor
from quapylab.persistence.database import Database
from quapylab.persistence.models import Group, GroupMembership, GroupType, MembershipRole, User


def bootstrap_desktop_workspace(database: Database) -> None:
    """Create the stable local user/group identity once, without a login flow."""
    actor = desktop_actor()
    user_id, group_id = str(actor.user_id), str(actor.group_id)
    with database.transaction() as session:
        if session.get(User, user_id) is None:
            session.add(
                User(
                    id=user_id,
                    login_name="local",
                    display_name="Local user",
                    is_system_administrator=False,
                )
            )
        if session.get(Group, group_id) is None:
            session.add(
                Group(
                    id=group_id,
                    name="Personal workspace",
                    slug="local-personal-workspace",
                    type=GroupType.PERSONAL,
                )
            )
        membership = session.scalar(
            select(GroupMembership).where(
                GroupMembership.group_id == group_id,
                GroupMembership.user_id == user_id,
            )
        )
        if membership is None:
            session.add(
                GroupMembership(
                    group_id=group_id,
                    user_id=user_id,
                    role=MembershipRole.OWNER,
                )
            )
