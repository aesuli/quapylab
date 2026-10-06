# ADR 0006: Internal frontend contracts

- Status: accepted
- Date: 2026-10-06

## Context

The React application and Flask services evolve independently, but the first
release does not promise a public automation API. The frontend must still be
able to safely render node configuration and actionable validation results.

## Decision

- The Flask API under `/api/v1` is an internal, versioned UI contract. It uses
  Pydantic request/response models and emits JSON Schema/OpenAPI during Phase 1.
  The React TypeScript client is generated from that checked contract.
- All success resources include stable UUIDs, explicit schema/version fields,
  and ISO-8601 UTC timestamps. Mutations use optimistic revision/version values
  where applicable. Errors use one JSON envelope with a machine-readable code,
  request ID, message safe for display, and field or graph-validation details.
- The node-type endpoint is server-owned. It returns type/version, display
  metadata, JSON Schema and UI hints, typed ports, capability predicates, and
  sensitive-field markers. The browser sends graph JSON only; it never submits
  executable module paths or callables.
- The UI performs responsive pre-validation, but server validation is
  authoritative. API visibility and disabled controls are usability features,
  never authorization checks.
- Run submission returns `202` and a run resource. The UI uses HTTP polling in
  Phase 1. Artifact downloads are authorized resource endpoints, not static
  paths. Cookie/session and CSRF mechanics belong to the web authentication
  implementation in Phase 1.
- Backward compatibility is maintained for the shipped frontend within a
  release. External clients receive no compatibility promise until the separate
  automation API is explicitly designed.

## Consequences

- Contract tests and generated-client checks are required in CI.
- New node and artifact fields can be added deliberately without coupling the UI
  to Python implementation types.
