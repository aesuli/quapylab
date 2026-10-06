"""Command-line entry points for QuaPy Lab."""

import argparse
from collections.abc import Sequence

from quapylab.app import create_app
from quapylab.persistence.database import Database
from quapylab.services.authentication import AuthenticationService


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
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "serve":
        create_app().run(host=args.host, port=args.port, debug=args.debug)
        return 0
    if args.command == "desktop":
        from quapylab.desktop import run_desktop

        return run_desktop(smoke_test=args.smoke_test)
    if args.command == "bootstrap-admin":
        database = Database(args.database_url)
        database.create_schema()
        with database.transaction() as session:
            user = AuthenticationService().create_user(
                session,
                login_name=args.login_name,
                display_name=args.display_name,
                password=args.password,
                is_system_administrator=True,
            )
        print(f"Created system administrator {user.login_name}.")
        return 0
    raise AssertionError(f"unhandled command: {args.command}")
