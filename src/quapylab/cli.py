"""Command-line entry points for QuaPy Lab."""

import argparse
from collections.abc import Sequence

from quapylab.app import create_app


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="quapylab")
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve = subparsers.add_parser("serve", help="start the local development server")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", default=8765, type=int)
    serve.add_argument("--debug", action="store_true")

    desktop = subparsers.add_parser("desktop", help="open QuaPy Lab in a native window")
    desktop.add_argument("--smoke-test", action="store_true", help=argparse.SUPPRESS)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "serve":
        create_app().run(host=args.host, port=args.port, debug=args.debug)
        return 0
    if args.command == "desktop":
        from quapylab.desktop import run_desktop

        return run_desktop(smoke_test=args.smoke_test)
    raise AssertionError(f"unhandled command: {args.command}")

