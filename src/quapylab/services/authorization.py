"""Role capabilities used by services before repository lookup or mutation."""

from enum import StrEnum

from quapylab.domain.actor import ActorContext
from quapylab.persistence.models import MembershipRole


class Permission(StrEnum):
    READ_PROJECT = "read_project"
    EDIT_PROJECT = "edit_project"
    RUN_EXPERIMENT = "run_experiment"
    MANAGE_MEMBERS = "manage_members"
    MANAGE_GROUP = "manage_group"


_ROLE_PERMISSIONS: dict[MembershipRole, frozenset[Permission]] = {
    MembershipRole.OWNER: frozenset(Permission),
    MembershipRole.ADMINISTRATOR: frozenset(Permission),
    MembershipRole.EDITOR: frozenset(
        {Permission.READ_PROJECT, Permission.EDIT_PROJECT, Permission.RUN_EXPERIMENT}
    ),
    MembershipRole.VIEWER: frozenset({Permission.READ_PROJECT}),
}


class AuthorizationError(PermissionError):
    """A deliberately non-disclosing authorization failure."""


def require_group_permission(
    actor: ActorContext,
    group_id: str,
    role: MembershipRole | None,
    permission: Permission,
) -> None:
    if actor.group_id.hex == group_id.replace("-", "") and actor.deployment_mode == "desktop":
        return
    if role is None or permission not in _ROLE_PERMISSIONS[role]:
        raise AuthorizationError("Resource not found.")
