"""Local-account authentication, revocable web sessions, and CSRF validation."""

from __future__ import annotations

import secrets
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import Lock
from uuid import UUID

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from sqlalchemy import select
from sqlalchemy.orm import Session

from quapylab.domain.actor import ActorContext
from quapylab.persistence.models import (
    Group,
    GroupMembership,
    GroupType,
    LocalCredential,
    MembershipRole,
    User,
    WebSession,
)


class AuthenticationError(PermissionError):
    """Raised without revealing whether an account exists."""


class CsrfError(PermissionError):
    """Raised when a state-changing browser request lacks a valid CSRF token."""


@dataclass(frozen=True, slots=True)
class AuthenticatedSession:
    session: WebSession
    actor: ActorContext


class LoginThrottle:
    """A small process-local throttle for local-account login attempts."""

    def __init__(self, attempts: int = 5, window: timedelta = timedelta(minutes=15)) -> None:
        self._attempts = attempts
        self._window = window
        self._failures: dict[str, deque[datetime]] = defaultdict(deque)
        self._lock = Lock()

    def is_limited(self, key: str, now: datetime) -> bool:
        with self._lock:
            return len(self._recent(key, now)) >= self._attempts

    def record_failure(self, key: str, now: datetime) -> None:
        with self._lock:
            self._recent(key, now).append(now)

    def reset(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)

    def _recent(self, key: str, now: datetime) -> deque[datetime]:
        failures = self._failures[key]
        cutoff = now - self._window
        while failures and failures[0] < cutoff:
            failures.popleft()
        return failures


class AuthenticationService:
    def __init__(self, session_lifetime: timedelta = timedelta(hours=12)) -> None:
        self._passwords = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=4)
        self._session_lifetime = session_lifetime

    def create_user(
        self,
        session: Session,
        *,
        login_name: str,
        display_name: str,
        password: str,
        is_system_administrator: bool = False,
    ) -> User:
        self._validate_password(password)
        normalized_login = login_name.strip().lower()
        if not normalized_login:
            raise ValueError("Login name is required.")
        if session.scalar(select(User.id).where(User.login_name == normalized_login)) is not None:
            raise ValueError("Login name is already in use.")
        user = User(
            login_name=normalized_login,
            display_name=display_name.strip() or normalized_login,
            is_system_administrator=is_system_administrator,
        )
        session.add(user)
        session.flush()
        session.add(LocalCredential(user_id=user.id, password_hash=self._passwords.hash(password)))
        group = Group(
            name=f"{user.display_name} personal workspace",
            slug=f"personal-{user.id}",
            type=GroupType.PERSONAL,
        )
        session.add(group)
        session.flush()
        session.add(GroupMembership(group_id=group.id, user_id=user.id, role=MembershipRole.OWNER))
        return user

    def authenticate(
        self, session: Session, *, login_name: str, password: str, now: datetime
    ) -> WebSession:
        user = session.scalar(select(User).where(User.login_name == login_name.strip().lower()))
        credential = session.get(LocalCredential, user.id) if user is not None else None
        if user is None or credential is None or not user.is_active:
            raise AuthenticationError("Invalid login credentials.")
        try:
            self._passwords.verify(credential.password_hash, password)
        except (InvalidHashError, VerifyMismatchError):
            raise AuthenticationError("Invalid login credentials.") from None
        if self._passwords.check_needs_rehash(credential.password_hash):
            credential.password_hash = self._passwords.hash(password)
        return self._new_session(session, user.id, now)

    def load_session(
        self, session: Session, *, token: str | None, now: datetime
    ) -> AuthenticatedSession:
        if not token:
            raise AuthenticationError("Authentication is required.")
        web_session = session.get(WebSession, token)
        if web_session is None or web_session.revoked_at is not None or _expired(web_session, now):
            raise AuthenticationError("Authentication is required.")
        user = session.get(User, web_session.user_id)
        if user is None or not user.is_active:
            raise AuthenticationError("Authentication is required.")
        membership = session.scalar(
            select(GroupMembership)
            .where(GroupMembership.user_id == user.id)
            .order_by(GroupMembership.created_at, GroupMembership.id)
        )
        if membership is None:
            raise AuthenticationError("Authentication is required.")
        web_session.last_activity_at = now
        return AuthenticatedSession(
            web_session,
            ActorContext(
                UUID(user.id), UUID(membership.group_id), "web", user.is_system_administrator
            ),
        )

    def revoke(self, web_session: WebSession, now: datetime) -> None:
        web_session.revoked_at = now

    def change_password(
        self, session: Session, *, user_id: str, current_password: str, new_password: str
    ) -> None:
        self._validate_password(new_password)
        credential = session.get(LocalCredential, user_id)
        if credential is None:
            raise AuthenticationError("Invalid login credentials.")
        try:
            self._passwords.verify(credential.password_hash, current_password)
        except (InvalidHashError, VerifyMismatchError):
            raise AuthenticationError("Invalid login credentials.") from None
        credential.password_hash = self._passwords.hash(new_password)
        credential.password_changed_at = datetime.now(UTC)
        credential.password_change_required = False

    def _new_session(self, session: Session, user_id: str, now: datetime) -> WebSession:
        web_session = WebSession(
            id=secrets.token_urlsafe(32),
            user_id=user_id,
            expires_at=now + self._session_lifetime,
            last_activity_at=now,
            csrf_token=secrets.token_urlsafe(32),
        )
        session.add(web_session)
        return web_session

    @staticmethod
    def _validate_password(password: str) -> None:
        if len(password) < 12:
            raise ValueError("Password must be at least 12 characters long.")


def utc_now() -> datetime:
    return datetime.now(UTC)


def _expired(web_session: WebSession, now: datetime) -> bool:
    expiry = web_session.expires_at
    return (expiry.replace(tzinfo=UTC) if expiry.tzinfo is None else expiry) <= now
