"""Command-line entry points for QuaPy Lab."""

import argparse
import time
from collections.abc import Sequence
from pathlib import Path

from quapylab.app import create_app
from quapylab.execution.backends import ProcessRunBackend
from quapylab.execution.local_worker import LocalWorker
from quapylab.execution.runtime_adapters import RuntimeNodeExecutor
from quapylab.nodes.registry import default_registry
from quapylab.persistence.database import Database
from quapylab.services.artifacts import ArtifactService, LocalArtifactStore
from quapylab.services.authentication import AuthenticationService
from quapylab.services.runs import RunService


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="quapylab")
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve = subparsers.add_parser("serve", help="start the local development server")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", default=8765, type=int)
    serve.add_argument("--debug", action="store_true")

    desktop = subparsers.add_parser("desktop", help="open QuaPy Lab in a native window")
    desktop.add_argument("--smoke-test", action="store_true", help=argparse.SUPPRESS)

    bootstrap = subparsers.add_parser("bootstrap-admin", help="create the first web administrator")
    bootstrap.add_argument("--database-url", required=True)
    bootstrap.add_argument("--login-name", required=True)
    bootstrap.add_argument("--display-name", required=True)
    bootstrap.add_argument("--password", required=True)

    set_password = subparsers.add_parser(
        "set-password", help="overwrite the password of an existing web user"
    )
    set_password.add_argument("--database-url", required=True)
    set_password.add_argument("--login-name", required=True)
    set_password.add_argument("--password", required=True)

    worker = subparsers.add_parser("worker", help="run the local workflow worker")
    worker.add_argument("--database-url", required=True)
    worker.add_argument("--once", action="store_true", help="process at most one queued run")
    worker.add_argument("--poll-seconds", default=1.0, type=float)
    worker.add_argument("--artifact-root", default=".quapylab/artifacts")
    worker.add_argument("--run-id", help="execute one specific queued run")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "serve":
        application = create_app()
        config = application.extensions["quapylab.config"]
        backend = ProcessRunBackend(
            config.database_url, application.extensions["quapylab.artifact_root"]
        )
        backend.recover()
        application.extensions["quapylab.run_backend"] = backend
        try:
            application.run(host=args.host, port=args.port, debug=args.debug)
        finally:
            backend.close()
        return 0
    if args.command == "desktop":
        from quapylab.desktop import run_desktop

        return run_desktop(smoke_test=args.smoke_test)
    if args.command == "bootstrap-admin":
        database = Database(args.database_url)
        database.create_schema()
        with database.transaction() as session:
            if AuthenticationService.find_user(session, args.login_name) is not None:
                print(
                    f"User {args.login_name} already exists; "
                    "use the set-password command to overwrite its password."
                )
                return 1
            user = AuthenticationService().create_user(
                session,
                login_name=args.login_name,
                display_name=args.display_name,
                password=args.password,
                is_system_administrator=True,
            )
        print(f"Created system administrator {user.login_name}.")
        return 0
    if args.command == "set-password":
        database = Database(args.database_url)
        database.create_schema()
        authentication = AuthenticationService()
        with database.transaction() as session:
            user = authentication.find_user(session, args.login_name)
            if user is None:
                print(f"User {args.login_name} does not exist.")
                return 1
            try:
                authentication.set_password(session, user_id=user.id, new_password=args.password)
            except ValueError as error:
                print(error)
                return 1
        print(f"Password updated for {user.login_name}; existing sessions were signed out.")
        return 0
    if args.command == "worker":
        if args.poll_seconds <= 0:
            raise ValueError("--poll-seconds must be greater than zero.")
        database = Database(args.database_url)
        database.create_schema()
        artifacts = ArtifactService(LocalArtifactStore(Path(args.artifact_root)))
        worker = LocalWorker(
            database, RunService(default_registry()), RuntimeNodeExecutor(artifacts)
        )
        recovered = worker.recover()
        if recovered:
            print(f"Marked {recovered} interrupted run(s).")
        if args.run_id:
            return 0 if worker.run(args.run_id) else 1
        if args.once:
            return 0 if worker.run_once() else 1
        while True:
            if not worker.run_once():
                time.sleep(args.poll_seconds)
    raise AssertionError(f"unhandled command: {args.command}")
