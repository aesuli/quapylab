# QuaPy Lab

QuaPy Lab is a visual workbench for reproducible quantification experiments backed by [QuaPy](https://github.com/HLT-ISTI/QuaPy).

It has two deployment modes: a passwordless personal desktop mode and an authenticated multi-user web mode where groups share projects through role-based permissions. 

See also [GOAL.md](GOAL.md) and [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md).

## Setup

Requirements:
- Python 3.11–3.13 (3.11 is the primary target)
- Node.js 20 or newer

Create and activate a Python 3.11 virtual environment, then install the project.

On Windows:

```powershell
py -3.11 -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev,desktop]"
```

On Linux:

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install -e '.[dev,desktop]'
```

The Linux desktop build also needs a pywebview-compatible GTK/WebKit or Qt runtime supplied by the distribution. The standalone server and WSGI modes do not require a desktop webview.

Run the backend:

```powershell
.venv\Scripts\python -m quapylab serve --debug
```

Run the authenticated multi-user web mode locally (SQLite is suitable for local
testing; supported multi-user deployments may require PostgreSQL).

On Windows, using powershell:

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

On Linux:

```bash
export QUAPYLAB_MODE="web"
export QUAPYLAB_DATABASE_URL="sqlite+pysqlite:///./.quapylab/web.sqlite3"
.venv/bin/python -m quapylab bootstrap-admin \
  --database-url "$QUAPYLAB_DATABASE_URL" \
  --login-name admin \
  --display-name "Administrator" \
  --password "choose-a-password"
.venv/bin/python -m quapylab serve --debug
```

The environment variables apply to commands run in that shell session.
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

Run the QuaPy Lab in a native window:

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

Build the Windows executable:

```powershell
.venv\Scripts\pyinstaller --noconfirm --clean packaging\QuaPyLab.spec
dist\QuaPyLab.exe --smoke-test
```

Build the Linux executable:

```bash
.venv/bin/pyinstaller --noconfirm --clean packaging/QuaPyLab.spec
dist/QuaPyLab --smoke-test
```

PyInstaller artifacts must be built on their target operating system. The command produces `dist/QuaPyLab.exe` on Windows and `dist/QuaPyLab` on Linux and macOS.

## Desktop releases

Each push to `main` creates a GitHub release with one platform-native archive for
Windows, Linux, and macOS. Each archive contains the single-file QuaPy Lab
executable, this README, and the license.

## License

See the [LICENSE](LICENSE) file.
