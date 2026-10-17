"""Explicit isolation boundary for synthetic development and browser fixtures."""
import os
from pathlib import Path


def fixture_mode() -> bool:
    return os.environ.get("RECIPE_IMPORTER_FIXTURE_MODE", "").strip().lower() in {
        "1", "true", "yes", "on",
    }


class FixtureModeError(RuntimeError):
    """Fixture UI is missing its explicit synthetic-data boundary."""


def fixture_output_dir() -> Path:
    """Require an explicitly selected, marked, non-default fixture library."""
    value = str(os.environ.get("RECIPE_IMPORTER_OUTPUT_DIR", "")).strip()
    if not value:
        raise FixtureModeError(
            "Demonstration mode needs an explicit isolated output folder marked with .fixture-library."
        )
    output = Path(value).expanduser().resolve()
    default_output = (Path.cwd() / "output").resolve()
    if output == default_output:
        raise FixtureModeError(
            "Demonstration mode cannot use the default output folder. Choose an isolated marked fixture folder."
        )
    marker = output / ".fixture-library"
    try:
        contents = marker.read_text(encoding="utf-8").casefold()
    except OSError as exc:
        raise FixtureModeError(
            "Demonstration mode needs an explicit isolated output folder marked with .fixture-library."
        ) from exc
    if "synthetic" not in contents and "demonstration" not in contents:
        raise FixtureModeError(
            "The selected demonstration folder is not marked as a synthetic fixture library."
        )
    return output


def fixture_source_allowed(source_path: str | Path) -> bool:
    """Permit previews only from the marked output tree or its sibling fixture sources."""
    try:
        output = fixture_output_dir()
        source = Path(source_path).expanduser().resolve()
    except (OSError, FixtureModeError, TypeError, ValueError):
        return False
    if _is_relative_to(source, output):
        return True
    source_value = str(os.environ.get("RECIPE_IMPORTER_SOURCE_DIR", "")).strip()
    if not source_value:
        return False
    source_root = Path(source_value).expanduser().resolve()
    if source_root.parent != output.parent or source_root == output:
        return False
    return _is_relative_to(source, source_root)


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def require_live_services() -> None:
    """Refuse remote clients in fixture mode, regardless of available secrets."""
    if fixture_mode():
        raise RuntimeError("Live services are disabled in fixture mode. Use an injected fake client.")
