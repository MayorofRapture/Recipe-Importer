"""One stable resolver for the local application library."""
from __future__ import annotations

import os
from pathlib import Path


class LibraryPathConflict(RuntimeError):
    """The launch directory contains a library that cannot be selected implicitly."""


def _application_root(app_root: str | Path | None) -> Path:
    return Path(app_root).expanduser().resolve() if app_root is not None else Path(__file__).resolve().parent


def _contains_data(path: Path) -> bool:
    try:
        return path.is_dir() and next(path.iterdir(), None) is not None
    except OSError:
        return False


def alternate_cwd_library(active: str | Path, cwd: str | Path | None = None) -> Path | None:
    """Return a different, populated `output` folder in the launch directory."""
    active_path = Path(active).expanduser().resolve()
    launch_root = Path(cwd).expanduser().resolve() if cwd is not None else Path.cwd().resolve()
    alternate = (launch_root / "output").resolve()
    return alternate if alternate != active_path and _contains_data(alternate) else None


def resolve_library_path(explicit: str | Path | None = None, *,
                         app_root: str | Path | None = None) -> Path:
    """Resolve fixture, CLI override, environment, then stable app-relative default.

    Relative `--output-dir` values are relative to the process CWD. Relative
    environment values are relative to the application root so GUI and CLI
    launches resolve the same setting from any working directory.
    """
    from runtime_safety import fixture_mode, fixture_output_dir

    if fixture_mode():
        return fixture_output_dir()

    root = _application_root(app_root)
    explicit_value = str(explicit or "").strip()
    configured_value = str(os.environ.get("RECIPE_IMPORTER_OUTPUT_DIR", "")).strip()
    if explicit_value:
        requested = Path(explicit_value).expanduser()
        if not requested.is_absolute():
            requested = Path.cwd() / requested
        return requested.resolve()
    if configured_value:
        requested = Path(configured_value).expanduser()
        if not requested.is_absolute():
            requested = root / requested
        return requested.resolve()

    default = (root / "output").resolve()
    alternate = alternate_cwd_library(default)
    if alternate is not None:
        raise LibraryPathConflict(
            f"A populated library exists at {alternate}, while the application default is {default}. "
            "No library was selected. Set RECIPE_IMPORTER_OUTPUT_DIR to the library you intend to use "
            "or pass --output-dir to the CLI; the application will not move or merge either library."
        )
    return default
