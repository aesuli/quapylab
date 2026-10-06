"""Repositories that require a group scope before returning project resources."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from quapylab.persistence.models import Project


class ProjectRepository:
    def __init__(self, session: Session, group_id: str) -> None:
        self._session = session
        self._group_id = group_id

    def add(self, project: Project) -> Project:
        if project.group_id != self._group_id:
            raise ValueError("Project does not belong to the authorized group scope.")
        self._session.add(project)
        return project

    def get(self, project_id: str) -> Project | None:
        return self._session.scalar(
            select(Project).where(Project.id == project_id, Project.group_id == self._group_id)
        )

    def list(self) -> list[Project]:
        return list(
            self._session.scalars(
                select(Project).where(Project.group_id == self._group_id).order_by(Project.name)
            )
        )
