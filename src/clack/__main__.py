"""Entry point for `python -m clack` and the `clack` CLI command."""

from __future__ import annotations

import argparse

from clack.app import ClackApp
from clack.version import get_version


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="clack",
        description="TUI for browsing, searching, and resuming Claude Code sessions.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"clack {get_version()}",
    )
    parser.parse_args(argv)

    app = ClackApp()
    app.run()


if __name__ == "__main__":
    main()
