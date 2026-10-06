from quapylab.persistence.database import Database
from quapylab.persistence.models import Group, GroupType, Project
from quapylab.persistence.repositories import ProjectRepository


def test_project_repository_does_not_cross_group_boundaries() -> None:
    database = Database("sqlite+pysqlite:///:memory:")
    database.create_schema()
    first_group = Group(name="First", slug="first", type=GroupType.PERSONAL)
    second_group = Group(name="Second", slug="second", type=GroupType.PERSONAL)

    with database.transaction() as session:
        session.add_all((first_group, second_group))
        session.flush()
        first_project = Project(group_id=first_group.id, name="Visible")
        second_project = Project(group_id=second_group.id, name="Hidden")
        ProjectRepository(session, first_group.id).add(first_project)
        ProjectRepository(session, second_group.id).add(second_project)
        session.flush()
        visible_id, hidden_id = first_project.id, second_project.id

    with database.transaction() as session:
        repository = ProjectRepository(session, first_group.id)
        assert repository.get(visible_id) is not None
        assert repository.get(hidden_id) is None
        assert [project.name for project in repository.list()] == ["Visible"]


def test_project_repository_rejects_writes_outside_its_scope() -> None:
    database = Database("sqlite+pysqlite:///:memory:")
    database.create_schema()
    group = Group(name="Scoped", slug="scoped", type=GroupType.PERSONAL)

    with database.transaction() as session:
        session.add(group)
        session.flush()
        repository = ProjectRepository(session, group.id)
        foreign_project = Project(group_id="other-group", name="Not allowed")
        try:
            repository.add(foreign_project)
        except ValueError as error:
            assert "authorized group scope" in str(error)
        else:
            raise AssertionError("Expected an out-of-scope project write to fail.")
