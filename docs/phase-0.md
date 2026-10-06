# Phase 0 validation log

## Compatibility baseline

- Target Python: 3.11, supported range 3.11–3.13.
- QuaPy: `>=0.2.2,<0.3`; the first compatibility run resolved and verified QuaPy 0.2.3.
- Node.js: 20 or newer.
- First native packaging target: Windows; Linux is required before the MVP release and must be built on Linux.

## Spikes

| Spike | State | Evidence / next action |
| --- | --- | --- |
| Flask application factory and internal API | Implemented | Liveness and version contract tests exist. |
| Direct QuaPy train/optimize/evaluate flow | Verified on Python 3.11.17 / QuaPy 0.2.3 | Deterministic labelled CSV fixture (1,200 rows, 12 features) was reloaded, split reproducibly, and completed `GridSearchQ` plus APP evaluation/reporting for ACC and PACC. Seed 42 results: ACC MAE 0.1203420092 / MRAE 4.8316550741; PACC MAE 0.0931612680 / MRAE 4.8940276423; 10 APP samples each. |
| Typed React Flow canvas | Verified | TypeScript and Vite production builds pass; typed connections are implemented. The project owner visually verified the native window on 2026-10-06. |
| Flask serves frontend bundle | Verified | Live HTTP checks returned the HTML entry point, hashed JavaScript asset, and health response. |
| pywebview hosting | Visually verified | The desktop entry point reuses the Flask application and passed hidden-window/package smoke checks. The project owner visually verified the native window on 2026-10-06. |
| PyInstaller one-file build | Verified for the architecture spike | The maintained spec produced a 14.7 MB Windows executable whose smoke path exited successfully. This size does not yet include the scientific stack because application services do not import QuaPy; the executable will grow materially when they do. |
| Linux compatibility | User-verified | On 2026-10-06, the project owner reported successful operation on a Linux machine. Automated Linux CI and release-artifact evidence should still be retained when available. |

Linux compatibility is accepted for the Phase 0 decision gate based on the project owner's successful machine test. Future releases still require automated Linux CI and native artifact smoke evidence because PyInstaller does not cross-compile and pywebview uses different native engines on Linux.

## Architecture decisions completed

- [ADR 0003](adr/0003-execution-backend.md) fixes process-backed execution, cancellation, recovery, and initiating-actor provenance.
- [ADR 0004](adr/0004-persistence-and-group-authorization.md) fixes the SQLite/PostgreSQL boundary and mandatory group-scoped authorization before Phase 1 authentication work.
- [ADR 0005](adr/0005-artifact-storage.md) fixes immutable, content-addressed artifact storage and its trust/isolation rules.
- [ADR 0006](adr/0006-frontend-contracts.md) fixes the internal API, error, graph, and generated TypeScript client contracts.

Phase 0 has no remaining acceptance criteria. The native canvas inspection also identified and corrected ambiguous port/type labelling: node identity now appears separately from explicit input and output contracts.

## Local environment observation

The initial machine inspection found Python 3.14.7 only. This is intentionally outside the declared project range because scientific dependencies must be proven together before expanding support. Node.js 24 is installed; PowerShell blocks the `npm.ps1` shim, so local commands should invoke `npm.cmd`.

