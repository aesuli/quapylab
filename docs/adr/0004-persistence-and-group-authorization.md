# ADR 0004: Persistence and group authorization boundary

- Status: accepted
- Date: 2026-10-06

## Context

Desktop use must work without a service database, while web deployments are
multi-user and need reliable concurrency, transactional membership changes, and
strong isolation of every project resource.

## Decision

- SQLAlchemy 2 and Alembic are the persistence and migration boundary.
- Desktop and development mode use SQLite. Supported multi-user web mode
  requires PostgreSQL; SQLite is never a supported collaborative deployment.
- `Group` is the mandatory ownership boundary. Each user has one personal group
  and may belong to shared groups. Every project belongs to exactly one group;
  datasets, workflow revisions, runs, node runs, reports, models, and artifact
  references reach their group through that project.
- Services accept `ActorContext` and authorize before resource lookup or
  mutation. Repositories require an authorized group scope and return a uniform
  not-found result for inaccessible opaque IDs. Client-provided user or group IDs
  are selectors only, never authority.
- Roles are owner, administrator, editor, and viewer as defined in ADR 0002.
  Ownership cannot be removed from the last owner. Project transfer is one
  transaction requiring permission in source and destination groups.
- Mutable rows use optimistic version fields. Membership removal, account
  disablement, session revocation, and ownership/role changes are transactional
  and create safe audit events. Authentication mechanics are deliberately Phase
  1; this ADR fixes the data and authorization boundary now.

## Consequences

- Tests must execute the same repository/service authorization matrix against
  SQLite and PostgreSQL, with PostgreSQL covering concurrent web semantics.
- System administrators administer identities and deployment policy but do not
  silently obtain project-data access; any support access is explicit and
  audited.
- Database backups and migrations must be coordinated with the artifact store.
