"""The identity and authorization context provided to domain services."""

from dataclasses import dataclass
from uuid import UUID, uuid5

_DESKTOP_NAMESPACE = UUID("248a60b6-ba5b-41c3-86df-037dd130b647")


@dataclass(frozen=True, slots=True)
class ActorContext:
    user_id: UUID
    group_id: UUID
    deployment_mode: str
    is_system_administrator: bool = False


def desktop_actor() -> ActorContext:
    """Return the stable, local-only actor and personal group for desktop mode."""
    return ActorContext(
        user_id=uuid5(_DESKTOP_NAMESPACE, "implicit-local-user"),
        group_id=uuid5(_DESKTOP_NAMESPACE, "implicit-personal-group"),
        deployment_mode="desktop",
    )
