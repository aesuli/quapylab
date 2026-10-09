# QuaPy Lab

QuaPy Lab is a local-first visual workbench for reproducible quantification experiments backed by [QuaPy](https://github.com/HLT-ISTI/QuaPy).

It has two deployment modes: a passwordless personal desktop mode and an authenticated multi-user web mode where groups share projects through role-based permissions. The foundation currently includes local accounts, server-side sessions, CSRF protection, group roles, projects, immutable workflow revisions, dataset artifacts, and a minimal queued runtime.

Phase 1 foundation work is underway. See [GOAL.md](GOAL.md) and [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) for the product boundary and roadmap.

## Development baseline

- Python 3.11–3.13 (3.11 is the primary target)
- Node.js 20 or newer
- Windows is the first development and native packaging target
- Standalone desktop releases are built natively for Windows, Linux, and macOS

Create and activate a Python 3.11 virtual environment, then install the project:

```powershell
py -3.11 -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev,desktop]"
```

On Linux:

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install -e '.[dev,desktop]'
```

The Linux desktop build also needs a pywebview-compatible GTK/WebKit or Qt runtime supplied by the distribution. The exact supported backend and package list will be pinned and tested in Linux CI before the MVP release. The standalone server and WSGI modes do not require a desktop webview.

Run the backend:

```powershell
.venv\Scripts\python -m quapylab serve --debug
```

Open the same application in a native window:

```powershell
.venv\Scripts\python -m quapylab desktop
```

Run its tests:

```powershell
.venv\Scripts\python -m pytest
```

Install and run the frontend:

```powershell
cd frontend
npm.cmd install
npm.cmd run dev
```

Run the direct QuaPy compatibility experiment:

```powershell
.venv\Scripts\python examples/reference_experiment.py
```

Build the Windows one-file architecture spike after building the frontend:

```powershell
.venv\Scripts\pyinstaller --noconfirm --clean packaging\QuaPyLab.spec
dist\QuaPyLab.exe --smoke-test
```

PyInstaller artifacts must be built on their target operating system. The maintained spec produces `dist/QuaPyLab.exe` on Windows and `dist/QuaPyLab` on Linux and macOS; release builds are validated in native CI rather than cross-compiled.

## Desktop releases

Each push to `main` creates a GitHub release with one platform-native archive for
Windows, Linux, and macOS. Each archive contains the single-file QuaPy Lab
executable, this README, and the AGPL-3.0-only license.
