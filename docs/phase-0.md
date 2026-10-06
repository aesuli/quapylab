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
| Direct QuaPy train/optimize/evaluate flow | Verified on Python 3.11.17 / QuaPy 0.2.3 | Deterministic synthetic data completed `GridSearchQ`, APP evaluation, and report generation. |
| Typed React Flow canvas | Built | TypeScript and Vite production builds pass; typed connections are implemented. Visual browser inspection was unavailable in the execution environment. |
| Flask serves frontend bundle | Verified | Live HTTP checks returned the HTML entry point, hashed JavaScript asset, and health response. |
| pywebview hosting | Runtime smoke-tested | The desktop entry point reuses the Flask application. The packaged native process remained healthy during a five-second hidden-window startup check; visible layout inspection remains. |
| PyInstaller one-file build | Verified for the architecture spike | The maintained spec produced a 14.7 MB Windows executable whose smoke path exited successfully. This size does not yet include the scientific stack because application services do not import QuaPy; the executable will grow materially when they do. |

The equivalent Linux source, WSGI, multiprocessing, pywebview, and PyInstaller checks remain required. They cannot be treated as passing based on the Windows result because PyInstaller does not cross-compile and pywebview uses different native engines on Linux.

## Local environment observation

The initial machine inspection found Python 3.14.7 only. This is intentionally outside the declared project range because scientific dependencies must be proven together before expanding support. Node.js 24 is installed; PowerShell blocks the `npm.ps1` shim, so local commands should invoke `npm.cmd`.

