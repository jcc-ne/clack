"""Version helpers for clack."""

from __future__ import annotations

import tomllib
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

PACKAGE_NAME = "clack-tui"


def get_version() -> str:
    """Return the installed package version."""
    try:
        installed_version = version(PACKAGE_NAME)
    except PackageNotFoundError:
        installed_version = None
    if installed_version:
        return installed_version

    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    try:
        project = tomllib.loads(pyproject.read_text())["project"]
    except (OSError, KeyError, tomllib.TOMLDecodeError):
        return "unknown"
    return str(project.get("version") or "unknown")
