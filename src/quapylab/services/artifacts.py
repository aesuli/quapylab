"""Immutable, content-addressed artifact persistence and authorized retrieval."""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from quapylab.domain.actor import ActorContext
from quapylab.persistence.models import Artifact
from quapylab.services.authorization import AuthorizationError, Permission
from quapylab.services.projects import ProjectService


class LocalArtifactStore:
    """A local store that exposes only SHA-256-derived, immutable object paths."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def key_for(digest: str) -> str:
        return f"sha256/{digest[:2]}/{digest[2:4]}/{digest}"

    def put(self, content: bytes) -> tuple[str, str]:
        digest = hashlib.sha256(content).hexdigest()
        key = self.key_for(digest)
        destination = self.root / key
        if destination.is_file():
            return digest, key
        destination.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(dir=destination.parent, prefix=".pending-")
        try:
            with os.fdopen(descriptor, "wb") as temporary:
                temporary.write(content)
                temporary.flush()
                os.fsync(temporary.fileno())
            try:
                os.replace(temporary_name, destination)
            except FileExistsError:
                # Another process committed the same immutable object first.
                pass
        finally:
            if os.path.exists(temporary_name):
                os.unlink(temporary_name)
        return digest, key

    def read(self, storage_key: str, expected_digest: str) -> bytes:
        path = self.root / storage_key
        try:
            content = path.read_bytes()
        except FileNotFoundError as error:
            raise ValueError("Artifact content is unavailable.") from error
        if hashlib.sha256(content).hexdigest() != expected_digest:
            raise ValueError("Artifact content failed its integrity check.")
        return content


class ArtifactService:
    def __init__(self, store: LocalArtifactStore) -> None:
        self._store = store
        self._projects = ProjectService()

    def create(
        self,
        session: Session,
        *,
        group_id: str,
        kind: str,
        media_type: str,
        content: bytes,
        serialization: dict[str, object],
        producing_run_id: str | None = None,
        producing_node_run_id: str | None = None,
    ) -> Artifact:
        digest, storage_key = self._store.put(content)
        artifact = session.scalar(
            select(Artifact).where(Artifact.group_id == group_id, Artifact.sha256 == digest)
        )
        if artifact is not None:
            # Content addressing intentionally reuses identical immutable bytes.  Preserve an
            # existing producer, but backfill provenance for legacy/unattributed content.
            if artifact.producing_run_id is None and producing_run_id is not None:
                artifact.producing_run_id = producing_run_id
                artifact.producing_node_run_id = producing_node_run_id
            return artifact
        artifact = Artifact(
            group_id=group_id,
            producing_run_id=producing_run_id,
            producing_node_run_id=producing_node_run_id,
            kind=kind,
            media_type=media_type,
            size_bytes=len(content),
            sha256=digest,
            storage_key=storage_key,
            serialization=serialization,
        )
        session.add(artifact)
        session.flush()
        return artifact

    def read_for_worker(self, artifact: Artifact) -> bytes:
        """Read an already-authorized artifact and verify its immutable digest."""
        return self._store.read(artifact.storage_key, artifact.sha256)

    def get(self, session: Session, *, actor: ActorContext, artifact_id: str) -> Artifact:
        artifact = session.get(Artifact, artifact_id)
        if artifact is None:
            raise AuthorizationError("Resource not found.")
        self._projects._require(session, actor, artifact.group_id, Permission.READ_PROJECT)
        return artifact

    def read(
        self, session: Session, *, actor: ActorContext, artifact_id: str
    ) -> tuple[Artifact, bytes]:
        artifact = self.get(session, actor=actor, artifact_id=artifact_id)
        # Check authorization before touching the object store.
        return artifact, self._store.read(artifact.storage_key, artifact.sha256)
