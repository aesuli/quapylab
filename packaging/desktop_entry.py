"""PyInstaller entry point for the native QuaPy Lab application."""

import sys

from quapylab.cli import main


def frozen_main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    # A frozen executable is both the desktop host and the isolated worker binary.
    # ProcessRunBackend invokes it with an explicit worker subcommand.
    if arguments and arguments[0] == "worker":
        return main(arguments)
    return main(["desktop", *arguments])


if __name__ == "__main__":
    raise SystemExit(frozen_main())
