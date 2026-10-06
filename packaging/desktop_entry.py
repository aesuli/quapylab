"""PyInstaller entry point for the native QuaPy Lab application."""

from quapylab.cli import main

if __name__ == "__main__":
    raise SystemExit(main(["desktop", *(__import__("sys").argv[1:])]))

