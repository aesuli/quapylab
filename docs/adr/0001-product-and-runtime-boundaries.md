# ADR 0001: Product and runtime boundaries

- Status: accepted
- Date: 2026-10-06

## Context

QuaPy Lab must offer a visual web interface, run locally or under WSGI, and support native-window distributions on Windows and Linux. Quantification training and evaluation are CPU-heavy and must not execute in request handlers. Production inference has different stability, security, and scaling requirements from an experimental workbench.

## Decision

- Use a Flask application factory as the WSGI boundary.
- Use a React/TypeScript frontend and React Flow for the workflow editor.
- Treat Windows and Linux as required platforms, with native builds and tests executed independently on each operating system.
- Treat the Lab's HTTP API as comprehensive but internal during the MVP.
- Execute experiments in separate worker processes behind a `RunBackend` abstraction.
- Keep production inference outside QuaPy Lab. A future QuaPy Model Server will consume versioned, integrity-checked model bundles exported by the Lab.
- Keep any future stable Lab automation API distinct from both the internal UI API and Model Server API.

## Consequences

- The MVP can use portable HTTP polling rather than WebSockets.
- A frontend build toolchain is required, but the installed runtime remains Python-hosted.
- PyInstaller and pywebview behavior cannot be inferred across operating systems; CI needs native Windows and Linux packaging lanes, and Linux webview system dependencies must be documented.
- Model-bundle compatibility becomes the main contract between experimentation and serving.
- The Lab remains focused and avoids prematurely committing its internal endpoints as a public integration surface.

