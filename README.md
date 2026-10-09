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

Run the authenticated multi-user web mode locally (SQLite is suitable for local
testing; supported multi-user deployments require PostgreSQL):

```powershell
$env:QUAPYLAB_MODE = "web"
$env:QUAPYLAB_DATABASE_URL = "sqlite+pysqlite:///./.quapylab/web.sqlite3"
.venv\Scripts\python -m quapylab bootstrap-admin `
  --database-url $env:QUAPYLAB_DATABASE_URL `
  --login-name admin `
  --display-name "Administrator" `
  --password "choose-a-password"
.venv\Scripts\python -m quapylab serve --debug
```

The environment variables apply to commands run in that PowerShell session.
`serve` does not prompt for a password; create the initial administrator with
`bootstrap-admin` first, then sign in at `http://127.0.0.1:8765`.

User management in web mode:

- Administrators open **Account** in the top bar to create and delete users and
  to reset any user's password (the target is signed out).
- Every user can change their own password from **Account**.
- Passwords must have at least 12 characters.
- Deleting a user is refused for yourself and for sole owners of a shared group;
  the user's personal workspace becomes inaccessible.

Set or overwrite a password from the command line (also signs the user out):

```powershell
.venv\Scripts\python -m quapylab set-password `
  --database-url $env:QUAPYLAB_DATABASE_URL `
  --login-name admin `
  --password "a-new-password"
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
