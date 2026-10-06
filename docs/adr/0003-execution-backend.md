# ADR 0003: Execution backend and run ownership

- Status: accepted
- Date: 2026-10-06

## Context

QuaPy training, model selection, and evaluation are CPU- and memory-intensive.
They can fail or be cancelled independently of a browser request and must retain
reproducible provenance in both desktop and multi-user web deployments.

## Decision

- The application service validates and compiles a workflow revision, creates a
  `Run`, then submits it through a `RunBackend` interface. HTTP request threads
  never execute QuaPy work.
- The Phase 1 backend is a local, dedicated process per run. It receives an
  immutable execution-plan reference, not arbitrary client Python or a live
  database session.
- Run and node states follow the documented state machines. Workers atomically
  persist state transitions, bounded structured logs, heartbeats, resolved
  configuration, environment snapshot, timings, and output artifact references.
- Cancellation is cooperative first; after a bounded grace period the dedicated
  process may be terminated. Expired heartbeats are recovered as `interrupted`.
- The run records the initiating `ActorContext`, workflow revision, dataset
  versions, seed, node-definition versions, and dependency versions. Outputs are
  owned by the workflow's project group, not by the worker or initiating user.
- A later Redis/distributed implementation must satisfy the same interface and
  persistence semantics; it is not part of Phase 0 or Phase 1.

## Consequences

- The UI polls run resources and can remain WSGI-compatible.
- Process isolation avoids blocking web workers and limits crashes, but requires
  explicit process lifecycle, serialization, cancellation, and recovery tests.
- Removing a member blocks future actions; an in-flight run keeps its recorded
  initiator for auditability but cannot move ownership outside its project group.
