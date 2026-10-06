# ADR 0005: Artifact storage and trust boundary

- Status: accepted
- Date: 2026-10-06

## Context

Datasets, reports, plots, splits, fitted models, and logs are larger and more
varied than relational metadata. They must be immutable evidence of a run while
remaining private to the project group that produced them.

## Decision

- Store artifact metadata in the database and bytes through an `ArtifactStore`
  interface. Phase 1 provides a local application-data-directory implementation;
  future object storage is an adapter, not a second domain model.
- Addresses are SHA-256 content hashes. Writes stream to a temporary file while
  hashing, validate size/media type/serialization metadata, then atomically
  rename into the content-addressed store. The database reference is committed
  only after the bytes are durable.
- Artifact records carry kind, media type, size, hash, storage key, serializer
  metadata, producing run/node, and owning project group. Data uses explicit
  formats such as JSON, Parquet/NPZ, CSV, PNG, and worker-produced joblib.
- A physical hash may be deduplicated, but authorization metadata is always
  group-scoped. No lookup, cache hit, error, URL, or timing may disclose whether
  another group has the same content.
- Downloads resolve through an authorized artifact reference; storage keys and
  filesystem paths are never API identifiers. Untrusted pickle/joblib uploads
  are never deserialized. Only locally produced, hash-verified trusted model
  artifacts may be loaded.
- Garbage collection removes only database-unreferenced bytes after a grace
  period. Database and artifact storage are backed up and restored as one
  consistency set.

## Consequences

- Ingestion and worker output need failure-safe cleanup and orphan reconciliation.
- Object storage can be adopted later without changing workflow/run provenance or
  authorization semantics.
