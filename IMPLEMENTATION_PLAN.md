# QuaPy Lab implementation plan

## 1. Product definition

QuaPy Lab is a local-first visual workbench for building, executing, comparing, and interactively applying quantification workflows backed by QuaPy. A user composes a typed directed acyclic graph (DAG) from dataset, preprocessing, classifier, quantifier, protocol, optimization, evaluation, reporting, and export nodes. The same saved workflow is both an editable experiment specification and a reproducible execution record.

The first release should make this path complete:

1. Import a labelled tabular dataset.
2. Inspect and validate its schema and class distribution.
3. Split it reproducibly.
4. Select a classifier and one or more QuaPy quantifiers.
5. Optionally optimize their hyperparameters with a quantification protocol.
6. Evaluate them on generated samples with selected metrics.
7. Compare results, timings, parameters, and plots.
8. Save/export the fitted model.
9. Interactively upload an unlabelled batch and inspect the fitted model's prevalence estimates.

Text, sparse, image, ordinal, confidence-region, and remote/distributed execution support should follow without requiring a redesign.

### Primary users

- Researchers comparing quantification methods under controlled protocols.
- Data scientists fitting a quantifier and validating it on unlabelled batches before export.
- Instructors and students learning quantification through inspectable workflows.
- Research teams sharing datasets, workflows, runs, reports, and fitted models inside controlled groups.

### Product principles

- Reproducible by default: inputs, package versions, seeds, resolved parameters, and artifacts belong to every run.
- Safe visual composition: incompatible ports and invalid configurations are rejected before execution.
- QuaPy-native semantics: preserve QuaPy concepts and outputs rather than inventing competing abstractions.
- Mode-aware without divergent business logic: desktop uses an implicit local identity; web deployments authenticate multiple users and authorize access through groups.
- Inspectable execution: a node exposes status, logs, duration, outputs, and failure details.
- Immutable evidence: completed run inputs and results cannot silently change when a dataset or workflow is edited.

## 2. Scope

### MVP (first useful release)

- Authenticated web accounts plus passwordless personal desktop mode.
- Personal and shared groups with owner, administrator, editor, and viewer membership roles.
- Group-owned project and workflow create/open/rename/duplicate/delete, member management, and project transfer between authorized groups.
- Node canvas with palette, search, typed connectors, configuration forms, validation, undo/redo, auto-layout, zoom/minimap, and JSON import/export.
- CSV/TSV import for dense tabular labelled and unlabelled datasets.
- Dataset preview, inferred schema, explicit feature/label selection, class mapping, missing-value summary, and train/validation/test split.
- Built-in QuaPy dataset loader nodes for a small curated set useful in examples/tests.
- Preprocessing through allow-listed scikit-learn transformers and pipelines.
- Classifier selection through an allow-listed scikit-learn registry.
- Initial QuaPy registry: CC, PCC, ACC, PACC, EMQ, HDy, DMy/DMx where compatible, plus a simple meta-method after the base path is stable.
- Protocols: APP, NPP, and evaluation on explicit labelled samples/collections.
- Model selection with `GridSearchQ`, parameter-grid editing, refit, seed, and worker-count settings.
- Metrics: AE/MAE, RAE/MRAE, SE/MSE, KLD/MKLD, NKLD/MNKLD, NAE/MNAE, and NRAE/MNRAE when supported by the installed QuaPy version.
- Run queue with cancellation, logs, per-node state, persisted outputs, and recovery of interrupted runs.
- Result tables, method ranking, timing breakdown, binary diagonal/bias plots, error-by-drift plots, generic prevalence/error charts, and CSV/JSON/PNG export.
- Fitted-model save/load, interactive prevalence prediction for an uploaded unlabelled batch, and a portable model-bundle export.
- An internal HTTP API covering the complete Lab UI: datasets, workflows, runs, logs, results, artifacts, models, and downloads. It is not an initially supported public integration API.
- Three launch modes: development, standalone localhost, and WSGI.
- Native desktop wrapper and platform-specific distributions for both Windows and Linux. Windows is validated first, but Linux support is required for the MVP release.

### Next releases

- Text and sparse-matrix ingestion/preprocessing.
- LeQua datasets and sample-protocol imports.
- Multiclass simplex visualization and confidence regions.
- Image embeddings and QuaNet/PyTorch as optional extras.
- Object-storage adapters and a Redis-backed distributed worker.
- External identity-provider login through OpenID Connect, enterprise group synchronization, and fine-grained project exceptions beyond group membership.
- Plugin SDK for third-party nodes and quantifiers.
- Experiment templates, reusable subgraphs, parameter sweeps across workflows, and a CLI/headless runner.
- A stable automation API for scripts and CI that covers the complete experiment lifecycle.
- A separately packaged QuaPy Model Server for authenticated, scalable production inference, followed later by scheduled or webhook-triggered operation if needed.

### Explicit non-goals for MVP

- Arbitrary Python execution in graph nodes.
- General-purpose ETL or a replacement for notebooks.
- Real-time multi-user canvas collaboration.
- Anonymous or public-link project sharing.
- Continuous data ingestion, externally triggered jobs, and production serving.
- A compatibility guarantee for the Lab's internal HTTP API to external clients.
- In-browser execution of QuaPy.
- Guaranteed support for every QuaPy method in the initial registry.

## 3. Recommended architecture

### Technology choices

| Area | Choice | Reason |
| --- | --- | --- |
| Backend | Python 3.11+, Flask application factory | Native WSGI deployment, simple embedding in pywebview, mature request/file handling. |
| API/schema | Flask + Pydantic v2 models | Explicit request, workflow, node, and artifact contracts; JSON Schema can drive UI forms. |
| ORM/migrations | SQLAlchemy 2 + Alembic | SQLite for desktop/development; PostgreSQL is required for supported multi-user web deployments. |
| Frontend | React + TypeScript + Vite | Strong ecosystem and a static production bundle served by Flask. |
| Graph editor | `@xyflow/react` (React Flow) | MIT licensed; custom nodes, handles, pan/zoom, selection, minimap, and controls are already provided. |
| Forms/state | React Hook Form + generated JSON Schema metadata; Zustand for editor state | Keeps node definitions server-owned while retaining responsive canvas state. |
| Tables/charts | TanStack Table + Plotly.js | Interactive comparison tables and exportable scientific charts. QuaPy/Matplotlib images can also be retained as artifacts. |
| Execution | Local process workers behind a `RunBackend` interface | ML tasks are CPU-heavy and must not run in WSGI request threads; process isolation enables cancellation and contains crashes. |
| Metadata | SQLite in desktop mode; PostgreSQL in web mode | Zero-service personal desktop operation while retaining concurrency, locking, and operational safety for multiple web users/workers. |
| Artifacts | Content-addressed files under an application data directory | Avoids putting datasets/models in SQLite and permits deduplication/integrity checking. |
| Serialization | JSON for specs/results, Parquet/NPZ for data, joblib for trusted local models | Match each artifact to its data shape; record formats and hashes. Never load untrusted joblib files. |
| Desktop | pywebview pointing at the same local Flask app | Native window without a separate desktop implementation; use the Windows WebView2 backend and a documented GTK/WebKit or Qt backend on Linux. |
| Packaging | PyInstaller, separate Windows and Linux CI builds | PyInstaller does not cross-compile; use one maintained cross-platform spec where possible and smoke-test each native artifact on its target OS. |
| Quality | pytest, Hypothesis, Ruff, mypy, Vitest, Testing Library, Playwright | Unit, contract, property, component, and end-to-end coverage. |

Flask is preferred over an ASGI-first framework because WSGI is an explicit requirement. The MVP should use ordinary HTTP polling for run progress (`GET /api/runs/{id}`) so it works under any conforming WSGI server. A future optional ASGI gateway may add WebSockets without changing domain services.

### Product and API boundaries

QuaPy Lab has an internal HTTP API because its React UI and Python backend are separate layers. That API is comprehensive but application-private in the MVP: web-session authentication and authorization are required, but the API does not promise token authentication for external clients, rate limits for public integrations, idempotent remote commands, or long-term client compatibility.

The application has two explicit identity modes:

- **Desktop mode:** creates or reuses one implicit local user and private group. No login screen or group administration is shown. It binds only to loopback and uses a per-launch secret to prevent unrelated local pages from invoking the API.
- **Web mode:** requires authentication for every non-health application route. Accounts are initially local and administrator-created; passwords are hashed with Argon2id. Browser authentication uses server-side sessions in secure, HTTP-only, SameSite cookies with CSRF protection. OpenID Connect is a later `AuthBackend`, not a prerequisite for the first release.

Domain services always receive an `ActorContext` containing the user, active group, deployment mode, and system privileges. Authorization is enforced in services and repositories before resource lookup or mutation; frontend visibility is never the security boundary.

Two later interfaces must remain distinct:

- **Lab automation API:** an optional, stable and authenticated facade for scripts or CI. It covers complete Lab operations—dataset upload, workflow revision selection, experiment submission/cancellation, status, and result/artifact download—not dataset ingestion alone.
- **QuaPy Model Server:** a separate deployable application that consumes a versioned model bundle exported by the Lab, validates inference batches, and returns prevalences. It contains no workflow editor, experiment scheduler, model training, or dataset catalogue. The server may share the bundle schema and a small runtime package with the Lab, but not the Lab database or application services.

```text
QuaPy Lab
data -> visual workflow -> training/evaluation -> report -> signed model bundle
                                                           |
                                                           v
                                                QuaPy Model Server
                                                production prediction
```

### Component boundaries

```text
React/React Flow UI
        |
        | versioned JSON/HTTP API
        v
Flask web application (WSGI)
  routes -> application services -> repositories
                    |                  |-- SQLite/PostgreSQL
                    |                  `-- artifact store
                    v
              RunBackend interface
                    |
             local worker process
                    |
        graph compiler/executor -> node registry -> QuaPy/scikit-learn
```

The web layer must not import concrete QuaPy classes directly. It accepts validated specifications and delegates to application services. The node registry and executor own runtime construction. This separation keeps HTTP, CLI, tests, and future distributed workers on the same execution path.

### Proposed repository layout

```text
pyproject.toml
src/quapylab/
  app.py                  # create_app(config) -> Flask
  config.py
  api/                    # blueprints, request/response mapping
  domain/                 # framework-independent entities and value objects
  services/               # workflow, dataset, run, model services
  persistence/            # SQLAlchemy models/repositories/migrations
  artifacts/              # store interface and local implementation
  nodes/                  # registry, definitions, built-in node adapters
  execution/              # compiler, scheduler, worker entry point
  quapy_adapter/           # allow-listed construction and result normalization
  desktop.py              # pywebview launcher
  cli.py
  static/                 # packaged frontend build
frontend/
  src/{api,components,features,stores,types}/
tests/{unit,integration,contract,e2e}/
examples/
packaging/
```

## 4. Core domain model

### Persistent entities

- `User`: login identity, display name, status, system-administrator flag, and timestamps.
- `LocalCredential`: user, Argon2id password hash and parameters, password-change requirement, and rotation timestamps.
- `Group`: project-sharing boundary, name, slug, type (`personal` or `shared`), and timestamps.
- `GroupMembership`: user, group, role (`owner`, `administrator`, `editor`, or `viewer`), inviter, and timestamps.
- `WebSession`: opaque server-side session identifier, user, expiry, last activity, and revocation metadata.
- `AuditEvent`: actor, group, project/resource reference, action, outcome, request id, timestamp, and safe structured details.
- `Project`: owning group, name, description, settings, timestamps.
- `Workflow`: project, name, mutable draft revision pointer.
- `WorkflowRevision`: immutable graph JSON, schema version, content hash, creation metadata.
- `Dataset`: logical identity, name, label status, current version.
- `DatasetVersion`: immutable schema, row count, classes, feature metadata, source, parent version, artifact hashes, ingestion report.
- `Run`: workflow revision, dataset versions, status, requested/cancelled timestamps, seed, environment snapshot, error summary.
- `NodeRun`: node id/type/version, state, resolved configuration, cache key, start/end time, logs, error, output artifact references.
- `Artifact`: kind, media type, size, SHA-256, storage key, producing run/node, serialization metadata.
- `Model`: friendly name, producing run, QuaPy/node definition versions, trusted model artifact, input signature, classes.

Use UUIDs at boundaries, UTC timestamps in storage, and optimistic revision/version fields on mutable records. Every project-scoped entity is reachable through an owning project/group; global artifact deduplication must never reveal another group's artifact existence or grant access across groups.

### Authentication and authorization

Every user receives a personal group on creation and may belong to shared groups. Projects belong to exactly one group, making the group the default sharing and retention boundary.

| Role | Capabilities within the group |
| --- | --- |
| Owner | All administrator capabilities; transfer or delete the group and assign ownership. At least one owner must remain. |
| Administrator | Invite, remove, and change non-owner members; create, transfer-in, archive, and delete projects; manage group settings. |
| Editor | Create projects; read and modify project datasets/workflows; run experiments; create reports/models; export allowed artifacts. |
| Viewer | Read projects, run history, reports, and permitted artifact downloads; cannot mutate or start/cancel runs. |

System administrators manage accounts and deployment policy but do not silently acquire access to project contents; any support access must be explicit and audited. Project transfer requires authorization in both source and destination groups. Membership removal immediately blocks new access and revokes active sessions when an account is disabled; already running jobs retain their recorded initiating actor but output remains owned by the project group.

Initial web-account lifecycle:

1. A one-time CLI command bootstraps the first system administrator.
2. System administrators create/disable accounts; group owners/administrators manage group membership.
3. Users change their own passwords; forgotten-password recovery is administrator-mediated until an email or OIDC backend exists.
4. Login, logout, password change, membership/role changes, project transfers/deletions, model export, and authorization failures create audit events without recording secrets or raw data.

### Workflow document

Every saved graph is a versioned JSON document containing:

- `schema_version`, workflow metadata, and editor viewport.
- Nodes with stable UUID, registered `type`, definition `version`, position, and JSON configuration.
- Edges with source node/port and target node/port.
- No executable module path or arbitrary callable from the client.

Node definitions are registered by the backend and returned from `GET /api/node-types`. Each definition contains display metadata, configuration JSON Schema/UI hints, input/output ports, data types, capability predicates, sensitive fields, and an executor factory.

Initial port types should be small and semantic:

- `labelled_collection`, `unlabelled_collection`, `dataset_split`
- `transformer`, `classifier_spec`, `quantifier_spec`, `fitted_quantifier`
- `protocol`, `parameter_grid`, `evaluation`, `prediction`, `report`

Do not model every Python type. Port compatibility belongs to a central type lattice, with exact matching in MVP and deliberately added safe coercions later.

### Graph validation and compilation

Validation runs in both client and server; the server is authoritative:

1. Validate document and node configurations against their schemas.
2. Reject unknown node types/versions and dangling or duplicate edges.
3. Verify port direction, type compatibility, and input cardinality.
4. Detect cycles with a topological sort.
5. Check required inputs and capability constraints (for example, binary-only methods or probabilistic classifier requirements).
6. Resolve defaults into an immutable execution plan.
7. Record warnings separately from blocking errors.

Compilation produces a topologically ordered plan with deterministic cache keys derived from node definition/version, resolved config, upstream artifact hashes, QuaPy/scikit-learn versions, and seed.

### Execution state machine

`queued -> preparing -> running -> succeeded | failed | cancelling -> cancelled`

Each node follows `pending -> ready -> running -> cached | succeeded | failed | skipped | cancelled`.

- A WSGI request only validates/enqueues and returns `202` with a run URL.
- The worker updates heartbeats and commits node transitions atomically.
- Cancellation is cooperative first, then terminates the dedicated worker process after a grace period.
- On application startup, expired heartbeats are marked `interrupted`; a user may retry from reusable completed artifacts.
- Logs are structured (`timestamp`, level, node id, event, message, fields) and bounded/rotated.
- Cache reuse is opt-in initially; models and splits are never reused unless all provenance inputs match.

QuaPy and libraries that use process parallelism must receive a controlled `n_jobs` to avoid nested oversubscription. The worker records wall time and, where practical, CPU time and peak memory.

## 5. Feature design

### Dataset management

The ingestion wizard should:

1. Stream uploads to a temporary file while computing a hash and enforcing a configurable size limit.
2. Detect delimiter/encoding conservatively and allow overrides.
3. Preview rows, infer types, and let the user designate ID, feature, label, and ignored columns.
4. Validate duplicate columns, invalid labels, empty classes, missing/infinite values, dimensional consistency, and minimum class support.
5. Convert to a canonical immutable artifact and create a `DatasetVersion` only after validation succeeds.

Splits must be represented as saved indices plus a seed, not only materialized copies. Prevent label leakage: preprocessing is fitted on training data and then applied to validation/test or interactive inference data.

Dataset uploads in the MVP are initiated by the Lab UI and create immutable versions. A run always binds to an exact dataset version, never the moving "latest" alias. Append-oriented ingestion, idempotency keys, and schema evolution policy belong to the later automation API only if a real integration use case requires them.

### QuaPy adapter and node library

Build an explicit registry rather than reflecting every importable class. Each entry declares:

- Stable public id and adapter version.
- QuaPy class/factory and supported QuaPy version range.
- Parameter schema, defaults, ranges, enums, and documentation URL.
- Binary/multiclass/ordinal and dense/sparse/text capabilities.
- Classifier requirements and optional dependencies.
- Fit/predict/evaluate adapters and normalized output schema.

Registry groups:

- Sources: upload, built-in QuaPy dataset, saved dataset/version.
- Data: schema/column selection, split, sample collection.
- Preprocessing: impute, scale, encode, feature selection, text vectorization (post-MVP).
- Classifiers: logistic regression, linear SVM/calibration, random forest; expose safe parameters only.
- Quantifiers: aggregative, non-aggregative, meta, neural (optional extra).
- Protocols: APP, NPP, explicit samples/labelled collection.
- Optimization: parameter grid and `GridSearchQ`.
- Evaluation: metrics and report generation.
- Outputs: comparison, plots, model save, interactive batch prediction, model-bundle export.

The adapter should retain QuaPy's optimized evaluation path by calling its public evaluation functions rather than manually predicting every sample. QuaPy reports true and estimated prevalences plus per-sample errors; persist this normalized long-form data so the UI can regenerate charts without rerunning the experiment.

### Experiment setup

- Support branching one dataset/protocol into several quantifiers and merging evaluations into a comparison report.
- Show a preflight summary: number of models/configurations/samples, expected task count, selected seed, and concurrency.
- Require an explicit validation source for model selection and keep final test data isolated.
- Make random seed, sample size, repeats, prevalence grid, replacement policy, and `n_jobs` visible in resolved configuration.
- Capture best parameters, all attempted configurations and statuses, validation scores, refit timing, and final test results.
- Allow a successful node or whole run to be cloned with configuration changes.

### Reports

Every comparison report should include:

- Dataset/version and split statistics; class order and training prevalence.
- Workflow revision, QuaPy/Python/dependency versions, platform, seed, and timestamps.
- Method/classifier names with fully resolved parameters.
- Aggregate metric table with mean, standard deviation, confidence interval where valid, rank, and failed sample count.
- Per-sample true/estimated prevalence and errors.
- Fit, optimization, prediction, evaluation, and total timings.
- Best parameters and optimization trace.
- Relevant binary diagonal/bias, error-by-drift, prevalence distribution, and multiclass plots.
- Machine-readable export alongside a self-contained HTML report; PDF can follow after HTML layout stabilizes.

For comparisons across paired samples, retain sample identifiers so later releases can add statistically valid paired tests rather than comparing only means.

### Fitted-model use

- Promote a successful fitted quantifier to the model library with its entire preprocessing pipeline and input signature.
- Validate an interactively uploaded unlabelled dataset against the signature and show actionable column/type errors.
- Display and export prevalence estimates in stored class order, with dataset/model versions, timestamps, and optional confidence output.
- Export a versioned model bundle containing the trusted model artifact, preprocessing pipeline, input signature, class order, dependency constraints, QuaPy Lab provenance, checksums, and a manifest suitable for a future QuaPy Model Server.
- Never deserialize a user-uploaded joblib/pickle artifact. Only load artifacts created locally by the trusted worker and verified by hash.

## 6. Internal HTTP API outline

The React frontend uses this complete internal API. Versioning its route prefix and schemas is still useful engineering discipline, but `/api/v1` is not a public compatibility promise in the MVP. Errors use one envelope with `code`, `message`, `details`, and `request_id`.

| Area | Representative endpoints |
| --- | --- |
| System | `GET /health/live`, `GET /health/ready`, `GET /version`, `GET /node-types` |
| Authentication | `POST /auth/login`, `POST /auth/logout`, `GET /auth/session`, `POST /auth/change-password` |
| Groups | `GET/POST /groups`, `GET/PATCH /groups/{id}`, `GET/POST /groups/{id}/members`, `PATCH/DELETE /groups/{id}/members/{user_id}` |
| Projects | `GET/POST /groups/{group_id}/projects`, `GET/PATCH/DELETE /projects/{id}`, `POST /projects/{id}/transfer` |
| Workflows | `GET/POST /workflows`, `GET/PATCH /workflows/{id}`, `POST /workflows/{id}/revisions`, `POST /workflows/{id}/validate` |
| Datasets | `GET/POST /datasets`, `GET /datasets/{id}`, `GET /dataset-versions/{id}/preview` |
| Runs | `POST /runs`, `GET /runs/{id}`, `POST /runs/{id}/cancel`, `GET /runs/{id}/nodes`, `GET /runs/{id}/logs` |
| Artifacts | `GET /artifacts/{id}`, `GET /artifacts/{id}/download` |
| Models | `GET/POST /models`, `GET /models/{id}`, `POST /models/{id}/predict`, `GET /models/{id}/bundle` |

Use multipart upload for files and JSON for metadata. Limit preview/result pagination. Set `Content-Disposition` safely on downloads and never accept a client-controlled filesystem path.

Standalone/desktop mode binds to `127.0.0.1` by default and uses an implicit local actor plus an unguessable per-launch secret between the embedded UI and local server. Web mode refuses to start without a persistent secret, PostgreSQL, an initialized administrator, trusted proxy/host configuration, and secure-cookie/TLS settings appropriate to its public URL. Mode is explicit startup configuration and cannot be selected from an HTTP request.

## 7. Delivery plan

Estimates assume one experienced full-time developer and include implementation plus automated tests, not long research pauses. Re-estimate after Phase 1. With web authentication and group sharing included, a credible Lab MVP is approximately 16–21 weeks; the automation API and QuaPy Model Server are separate follow-on projects.

### Phase 0 — decisions and spikes (1 week)

- Pin supported Python and QuaPy versions and define dependency extras (`core`, `text`, `neural`, `desktop`, `dev`).
- Prototype one QuaPy experiment end-to-end: CSV -> split -> ACC/PACC -> APP -> `GridSearchQ` -> report.
- Prototype React Flow custom nodes with typed connection validation.
- Prototype Flask served static bundle inside pywebview and a PyInstaller build.
- Measure frozen size/start time and identify hidden imports/native libraries.
- Produce architecture decision records for Flask/WSGI, execution backend, persistence, artifacts, and frontend.

Exit: each risky integration has a runnable spike and the version matrix/build targets are agreed.

### Phase 1 — foundation, identity, and contracts (3–4 weeks)

- Create Python/frontend packages, CI, lint/type/test commands, app factory, configuration profiles, and structured logging.
- Run backend and frontend checks in a Windows and Linux CI matrix from the start, even before native packaging is introduced.
- Define Pydantic workflow/node/artifact schemas and JSON error envelope.
- Implement registry skeleton, graph validation/topological compilation, and schema-version migration hooks.
- Add SQLAlchemy entities, Alembic baseline, repositories, local artifact store, and transactional service tests for both SQLite and PostgreSQL.
- Implement explicit desktop/web modes, `ActorContext`, local accounts with Argon2id, server-side sessions, CSRF protection, login throttling, administrator bootstrap CLI, groups, memberships, roles, and audit events.
- Apply group scoping and role checks in services/repositories and test horizontal-access attempts, disabled accounts, membership changes, project transfer, and last-owner invariants.
- Build login, account, active-group switcher, group membership, and permission-aware project UI; desktop mode hides these controls while using its implicit personal group.
- Generate a typed frontend API client from the published OpenAPI document or a checked contract.

Exit: desktop mode creates a usable implicit personal workspace without login; in web mode two authenticated users can share a group project according to their roles, cannot access projects outside their groups, and an API-created workflow revision survives restart and round-trips without information loss.

### Phase 2 — dataset vertical slice (2 weeks)

- Implement streamed CSV/TSV upload, preview, schema override, canonical conversion, dataset versioning, and split artifacts.
- Implement dataset pages and ingestion wizard.
- Add a built-in QuaPy dataset source for deterministic demos.
- Add upload limits, filename/path hardening, hashes, duplicate-content handling, and malformed-file tests.

Exit: a labelled dataset can be imported, inspected, versioned, split reproducibly, and retrieved through UI/API.

### Phase 3 — workflow editor (2–3 weeks)

- Implement node palette/search, custom node shell, typed handles, edge validation, inspector forms, and inline errors.
- Add selection, delete, duplicate, copy/paste, undo/redo, keyboard access, auto-layout, minimap, zoom, dirty-state warning, and autosave/manual revision creation.
- Load node definitions from the backend and preserve unknown future nodes as read-only placeholders during import.
- Add component and Playwright workflow-editing tests.

Exit: a user can visually build and save a valid minimal experiment; invalid cycles/types/required inputs are clearly blocked.

### Phase 4 — execution and QuaPy core (3 weeks)

- Implement local `RunBackend`, worker lifecycle/heartbeat/cancellation, state machines, logs, startup recovery, and polling.
- Add data, preprocessing, classifier, initial quantifier, APP/NPP, evaluation, and model-save adapters.
- Enforce deterministic seeds, controlled `n_jobs`, provenance capture, trusted serialization, and artifact integrity checks.
- Run jobs in separate processes and test failures, cancellation, interrupted recovery, and concurrency.

Exit: the visual CSV -> split -> classifier/quantifier -> APP -> MAE report flow executes outside the WSGI process and is reproducible.

### Phase 5 — optimization, comparison, reporting (2–3 weeks)

- Add parameter-grid UI and `GridSearchQ` adapter with progress/attempt records.
- Add multi-method branching and comparison merge.
- Build result tables, rankings, drill-down, timing views, plots, and CSV/JSON/PNG/self-contained HTML export.
- Add cache keys and conservative reuse for deterministic successful nodes.
- Validate numeric results against direct QuaPy reference scripts.

Exit: at least three quantifiers can be optimized and compared on the same generated samples, with all provenance and exports present.

### Phase 6 — model library and interactive validation (1 week)

- Implement model library/promotion, signature validation, interactive unlabelled-batch prediction, and result history.
- Define and implement the versioned model-bundle manifest and export, including hashes, provenance, dependency constraints, input signature, and class order.
- Threat-model uploads, serialization, local binding, and model-bundle integrity.

Exit: a user can validate an approved model on an uploaded unlabelled dataset and export a self-describing, integrity-checked bundle without exposing a production endpoint.

### Phase 7 — distribution and release hardening (2 weeks)

- Add CLI commands: `serve`, `desktop`, `worker`, `db upgrade`, and diagnostics.
- Document WSGI entry point and production configuration; test Waitress on Windows and Gunicorn on Linux.
- Complete the cross-platform pywebview launcher, lifecycle shutdown, single-instance/port selection, data-directory selection, and native file-dialog integration where useful.
- Document the supported Linux webview backend and its system packages; fail startup with an actionable diagnostic when they are missing.
- Maintain the PyInstaller spec, bundle frontend/migrations/licenses, and build separate Windows and Linux artifacts on native CI runners.
- Add source-install and frozen smoke tests on clean Windows and Linux runners, example workflows/data, backup/restore instructions, user guide, and release checklist.

Exit: source install, localhost server, WSGI deployment, and checksummed Windows and Linux native artifacts pass the same smoke workflow. If one-file mode is unreliable on either platform, a documented `onedir` artifact is an acceptable fallback for that platform.

## 8. Testing and acceptance strategy

### Test layers

- Unit: graph/type validation, schema inference, state transitions, cache keys, registry factories, metrics normalization.
- Property: arbitrary DAGs never bypass cycle/type checks; prevalence vectors remain finite, non-negative, and sum to one within tolerance.
- Contract: frontend/backend JSON schemas and API error shapes.
- Authorization matrix: exercise every protected operation for anonymous, disabled, viewer, editor, administrator, owner, and system-administrator actors.
- Integration: database + artifact store + worker + small QuaPy datasets; process cancellation and restart recovery.
- Golden/reference: compare QuaPy Lab outputs with small direct QuaPy scripts under pinned versions and seeds.
- UI: editor interactions, validation, progress polling, report rendering, accessibility keyboard path.
- Packaging: launch frozen executable in a clean VM, complete sample workflow, restart, and reload artifacts.
- Platform matrix: run core, integration, frontend, WSGI, filesystem/path, multiprocessing, and packaged smoke tests on both Windows and Linux.
- Performance: upload, graph compilation, UI with 100–200 nodes, result tables, worker memory, and concurrent-run limits.
- Security: credential/session handling, login throttling, CSRF, session fixation/revocation, horizontal and vertical privilege escalation, group/artifact isolation, malicious filenames/archives, oversized uploads, MIME mismatch, formula injection in CSV export, cross-origin/loopback request protection, and rejection of untrusted serialized models.

### MVP acceptance scenarios

1. On a clean machine, launch desktop mode without a login prompt and finish the bundled binary demo workflow inside its implicit personal group.
2. Import the same CSV and seed twice; splits and metrics are identical within documented numerical tolerance.
3. Connect incompatible ports or create a cycle; both UI and API reject the graph with node/edge-specific errors.
4. Run ACC, PACC, and EMQ on the same APP samples; compare metrics and timing and export the underlying per-sample data.
5. Cancel a long model-selection run; the UI reaches `cancelled`, partial logs remain available, and no run is left falsely active after restart.
6. Promote a fitted model, upload an unlabelled dataset interactively, and receive a valid class-prevalence vector; reject a mismatched schema clearly.
7. Export that model as a versioned bundle and verify its manifest, provenance, input signature, class order, dependency constraints, and artifact hashes without loading its executable payload.
8. Run behind a WSGI server with a non-root URL prefix and with no dependency on WebSockets.
9. Launch the packaged native app; it uses a protected private loopback server, opens one native window, and shuts down its child process cleanly.
10. Repeat the standalone, WSGI, worker-process, artifact-path, and native-package smoke workflows on both Windows and Linux.
11. In web mode, verify that an owner can create a group, add an editor and viewer, and share a project; the editor can modify/run it, the viewer can only read it, and an unrelated authenticated user receives the same non-disclosing denial as for an unknown resource.
12. Disable a web account or remove a membership and verify that access is revoked, sessions are invalidated as required, running-job provenance remains intact, and security-relevant actions appear in the audit trail.

## 9. Operational and security requirements

- Store mutable application data outside the installation directory using platform-standard application-data locations; allow explicit override.
- Never expose secrets, raw rows, or sensitive node fields in logs. Provide configurable retention and delete/export operations.
- Use content hashes and atomic rename for artifact commits; garbage-collect only unreferenced artifacts after a grace period.
- Back up the database and artifact directory as one consistency set; document restore and schema migration rollback boundaries.
- Default to one concurrent local run and expose a resource ceiling. Prevent nested all-core parallelism.
- `/health/ready` checks database and artifact-store writability, not expensive ML dependencies.
- Pin dependency ranges and record the exact resolved environment in each run. QuaPy adapter compatibility tests gate upgrades.
- Serve immutable hashed frontend assets with a strict Content Security Policy. Reject arbitrary node imports and executable expressions. Protect the native loopback API with a per-launch secret.
- Treat uploaded data as sensitive and local by default; no telemetry unless explicitly opt-in.
- Scope every query, cache lookup, artifact download, log stream, and background-job action to an authorized actor and group. Never trust a group or user id supplied by the client without checking membership.
- In web mode, require TLS at the deployment boundary, secure session cookies, CSRF protection, login throttling, password-strength policy, Argon2id hashes, secret rotation procedures, and auditable account administration.

## 10. Main risks and mitigations

| Risk | Mitigation |
| --- | --- |
| QuaPy methods have heterogeneous requirements and optional dependencies. | Explicit capability-rich registry; phased method coverage; extras; compatibility tests against a pinned QuaPy range. |
| Single-file bundles become large or miss dynamic/native dependencies. | Phase-0 spike, maintained PyInstaller hooks/spec, target-OS builds, clean-VM smoke tests; retain `onedir` as a supported fallback if one-file reliability is inadequate. |
| Long tasks block or outlive WSGI requests. | Separate worker processes, persistent state/heartbeats, polling, cancellation, and startup reconciliation. |
| SQLite contention or locking assumptions leak into collaborative deployment. | Confine SQLite to desktop/development mode; require PostgreSQL and transactional concurrency tests for supported multi-user web deployments. |
| A missing authorization check exposes another group's datasets, artifacts, logs, or models. | Central `ActorContext`, deny-by-default service/repository policies, group-scoped queries, opaque identifiers, uniform not-found responses, and exhaustive authorization-matrix tests. |
| Desktop and web modes drift into separate products. | Use the same entities and application services; desktop injects an implicit local actor/group, while only authentication and administration presentation differ. |
| Password/session handling creates avoidable security risk. | Argon2id, server-side revocable sessions, secure cookie/CSRF defaults, login throttling, administrator-mediated recovery initially, audit events, and an `AuthBackend` boundary for later OIDC. |
| Dataset mutation breaks reproducibility. | Immutable dataset versions and run binding; "latest" resolved at enqueue time and stored. |
| Pickle/joblib permits code execution. | Load only worker-produced hashed artifacts; never accept serialized model uploads; a future Model Server accepts only approved, integrity-checked Lab bundles through an administrative deployment path. |
| Lab endpoints accidentally become an unsupported production API. | Document them as internal, bind locally by default, and design the later automation API and Model Server as explicit, separately versioned contracts. |
| Leakage from preprocessing or optimization invalidates experiments. | Split-index artifacts, pipeline fit only on training inputs, validation/test role checks, provenance and reference tests. |
| Graph format or node definitions evolve. | Version workflow schema and each node definition; migrations plus read-only placeholders for unknown nodes. |
| Nested parallelism exhausts desktop resources. | Central resource policy passes bounded `n_jobs` and sets numerical-library thread limits per worker. |
| Scientific comparisons are misleading. | Preserve paired sample IDs, expose full distributions and failures, label descriptive statistics, add inferential tests only with documented assumptions. |

## 11. Decisions needed before Phase 2

Recommended defaults are shown first; these are product choices rather than blockers for Phase 0/1.

1. Deployment modes: passwordless single-user desktop and authenticated multi-user web are both MVP requirements; PostgreSQL is required for supported web deployment.
2. Platform order: implement and validate on Windows first, but require Linux source, WSGI, worker, and native-package tests before the MVP release; macOS may follow later.
3. Initial data scope: dense CSV/TSV tabular data; text/sparse support immediately after MVP.
4. Frontend: React/TypeScript with React Flow; accept a Node.js build-time toolchain while keeping the runtime application Python-hosted.
5. Workflow semantics: DAG only; loops/conditionals are out of scope.
6. WSGI progress transport: polling; no WebSocket requirement.
7. Artifact portability: exported workflow specs are portable; fitted models are guaranteed only across an explicitly compatible Python/dependency environment.
8. Licensing/distribution: confirm that all selected optional algorithms, solvers, fonts, and bundled binaries are compatible with the intended project license.

## 12. Follow-on products

These are deliberately outside the Lab MVP and need their own requirements and threat models.

### Lab automation API

Add only when scripts, CI, or remote integrations are an actual use case. Its stable API should cover dataset upload, workflow revision selection, run submission/cancellation, status/log retrieval, and result/artifact download as one coherent lifecycle. It requires authentication, scopes, idempotency, quotas, pagination, compatibility policy, and generated client examples.

### QuaPy Model Server

Start from the exported bundle contract, not the Lab database. The initial server should provide readiness/version/model-metadata endpoints and one authenticated batch-prediction operation. Deployment should atomically approve and activate bundles, verify checksums before loading, enforce the input signature and resource limits, expose operational metrics, and support rollback. Training and experiment management remain exclusively in QuaPy Lab.

## 13. First implementation backlog

These are the first independently reviewable changes after the Phase-0 spikes:

1. Initialize `pyproject.toml`, source package, frontend workspace, and unified developer commands.
2. Add `create_app`, `/health/live`, `/version`, configuration tests, and frontend static fallback.
3. Add explicit desktop/web configuration and an `ActorContext`; bootstrap the implicit desktop user and personal group.
4. Add SQLAlchemy/Alembic user, credential, group, membership, session, audit, project, workflow, revision, run, node-run, and artifact tables, exercising SQLite and PostgreSQL.
5. Implement local web authentication, administrator bootstrap, session/CSRF handling, group roles, centralized authorization policies, and their security matrix tests.
6. Build login, active-group, membership, and permission-aware project screens, hidden appropriately in desktop mode.
7. Define the versioned workflow schema and five minimal node definitions: dataset, split, classifier, ACC, evaluation.
8. Implement registry discovery without arbitrary imports and return definitions through `/api/v1/node-types`.
9. Implement server graph validation/topological sorting with detailed errors and property tests.
10. Add atomic content-addressed local artifact storage with hash verification and group-scoped authorization metadata.
11. Build the React Flow shell, palette, inspector, save/reload, and validation display.
12. Implement CSV ingestion/versioning and the small deterministic dataset fixture.
13. Implement the local process worker and the minimal QuaPy reference workflow with initiating-actor provenance.

## 14. Research basis

The plan was checked against current primary documentation on 2026-10-06:

- [QuaPy repository and feature overview](https://github.com/HLT-ISTI/QuaPy): QuaPy 0.2.2 covers datasets, quantifiers, protocols, metrics, model selection, uncertainty, and plots, with binary and single-label multiclass support.
- [QuaPy datasets manual](https://hlt-isti.github.io/QuaPy/manuals/datasets.html): `Dataset` and `LabelledCollection`, sampling, built-in UCI/Twitter/review/LeQua/image data, and persisted downloads.
- [QuaPy evaluation manual](https://hlt-isti.github.io/QuaPy/manuals/evaluation.html): `evaluate`, `evaluation_report`, per-sample true/estimated prevalences and errors, plus optimized aggregative evaluation.
- [QuaPy methods manual](https://hlt-isti.github.io/QuaPy/manuals/methods.html): method families, classifier/calibration requirements, binary/multiclass limitations, optional dependencies, meta-methods, and confidence-aware methods.
- [QuaPy model-selection manual](https://hlt-isti.github.io/QuaPy/manuals/model-selection.html): `GridSearchQ`, quantification-oriented loss, protocol-driven selection, best parameters/model, and refit.
- [QuaPy plotting manual](https://hlt-isti.github.io/QuaPy/manuals/plotting.html): binary diagonal/bias, error-by-drift, and simplex plots.
- [React Flow documentation](https://reactflow.dev/): MIT-licensed node editor primitives including custom React nodes, handles, selection, pan/zoom, minimap, and controls.
- [pywebview introduction](https://pywebview.idepy.com/en/guide/) and [application architecture](https://pywebview.idepy.com/en/guide/architecture): native system webviews, Python/JavaScript integration, and direct use of a local Flask/WSGI application.
- [PyInstaller documentation](https://pyinstaller.org/en/stable/): platform-specific freezing and one-file/one-folder distribution; frozen applications must be built for each target OS.

