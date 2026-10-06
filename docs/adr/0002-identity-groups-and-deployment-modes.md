# ADR 0002: Identity, groups, and deployment modes

- Status: accepted
- Date: 2026-10-06

## Context

QuaPy Lab must serve both personal native-desktop use and collaborative web use. Requiring authentication in the desktop application would add friction, while omitting identity from the domain would make secure project sharing difficult and encourage authorization checks to be scattered through HTTP routes.

## Decision

- Maintain one domain and application-service model with explicit `desktop` and `web` deployment modes.
- Every request or background command carries an `ActorContext`.
- Desktop mode injects one implicit local user and personal group, binds to loopback, and uses a per-launch secret. It exposes no login or group-administration UI.
- Web mode requires authentication. Initial accounts are local and administrator-created, with Argon2id password hashes and server-side revocable sessions protected by secure cookies and CSRF tokens.
- Every user has a personal group and may join shared groups.
- Every project belongs to exactly one group. Group membership roles are owner, administrator, editor, and viewer.
- Authorization is deny-by-default in application services and repositories, not merely in routes or frontend components.
- SQLite supports desktop/development mode. PostgreSQL is required for supported multi-user web deployments.
- OpenID Connect may later implement the authentication boundary without changing project ownership and authorization semantics.

## Consequences

- Dataset, workflow, run, artifact, report, and model queries must all be group-scoped.
- Content-addressed artifact deduplication may share physical bytes but never authorization metadata or existence information across groups.
- Background jobs retain the initiating actor for provenance while outputs remain owned by the project group.
- Account disabling, session revocation, membership removal, last-owner protection, and project transfer require explicit transactional behavior and tests.
- Web deployment now requires security configuration, PostgreSQL, administrator bootstrap, and audit logging; it is no longer a simple non-loopback variant of desktop mode.
- Real-time simultaneous editing remains out of scope; sharing provides controlled access to saved project state and revisions.
