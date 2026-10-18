"""Explicit isolation boundary for synthetic development and browser fixtures."""
import os
import json
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
    default_output = _production_output_dir()
    requested_output = Path(os.path.abspath(Path(value).expanduser()))
    if (_paths_overlap(output, default_output)
            or _contains_link(requested_output)):
        raise FixtureModeError(
            "Demonstration mode cannot use an output folder that overlaps or links into production. Choose an isolated marked fixture folder."
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
    if os.environ.get("RECIPE_IMPORTER_BROWSER_FIXTURE") == "1":
        validate_browser_fixture_environment()
    return output


def fixture_source_allowed(source_path: str | Path) -> bool:
    """Permit previews only from the marked output tree or its sibling fixture sources."""
    try:
        output = fixture_output_dir()
        requested_source = Path(os.path.abspath(Path(source_path).expanduser()))
        source = requested_source.resolve()
    except (OSError, FixtureModeError, TypeError, ValueError):
        return False
    if _contains_link(requested_source):
        return False
    if _is_relative_to(source, output):
        return True
    source_value = str(os.environ.get("RECIPE_IMPORTER_SOURCE_DIR", "")).strip()
    if not source_value:
        return False
    requested_root = Path(os.path.abspath(Path(source_value).expanduser()))
    if _contains_link(requested_root):
        return False
    source_root = requested_root.resolve()
    if source_root.parent != output.parent or source_root == output:
        return False
    return _is_relative_to(source, source_root)


def validate_browser_fixture_environment() -> str:
    """Fail closed unless a browser run has matching, isolated fixture paths and markers."""
    if not fixture_mode() or os.environ.get("RECIPE_IMPORTER_BROWSER_FIXTURE") != "1":
        raise FixtureModeError("The isolated browser fixture runtime is not enabled.")
    if any(os.environ.get(name, "").strip() for name in (
        "GEMINI_API_KEY", "MEALIE_URL", "MEALIE_TOKEN",
    )):
        raise FixtureModeError("Browser fixtures must not inherit live provider credentials.")

    values = {
        "root": os.environ.get("RECIPE_IMPORTER_FIXTURE_ROOT", "").strip(),
        "output": os.environ.get("RECIPE_IMPORTER_OUTPUT_DIR", "").strip(),
        "sources": os.environ.get("RECIPE_IMPORTER_SOURCE_DIR", "").strip(),
    }
    if any(not value or not Path(value).is_absolute() for value in values.values()):
        raise FixtureModeError("Browser fixtures require explicit absolute root, output, and source paths.")
    requested = {name: Path(os.path.abspath(value)) for name, value in values.items()}
    if any(_contains_link(path) for path in requested.values()):
        raise FixtureModeError("Browser fixture paths cannot traverse a symlink or junction.")
    try:
        resolved = {name: path.resolve(strict=True) for name, path in requested.items()}
    except OSError as exc:
        raise FixtureModeError("A browser fixture path is unavailable.") from exc
    if any(not path.is_dir() for path in resolved.values()):
        raise FixtureModeError("Browser fixture root, output, and source paths must be directories.")

    root, output, sources = resolved["root"], resolved["output"], resolved["sources"]
    production = _production_output_dir()
    if (not _is_relative_to(output, root) or not _is_relative_to(sources, root)
            or _paths_overlap(output, sources) or _paths_overlap(root, production)
            or _paths_overlap(output, production)):
        raise FixtureModeError("Browser fixture paths must stay in a disposable tree outside production output.")

    run_id = os.environ.get("RECIPE_IMPORTER_FIXTURE_RUN_ID", "").strip()
    nonce = os.environ.get("RECIPE_IMPORTER_FIXTURE_NONCE", "").strip()
    if not run_id or len(nonce) < 32:
        raise FixtureModeError("Browser fixture runtime attestation is missing or invalid.")
    expected = {"kind": "synthetic-browser-fixture", "run_id": run_id, "nonce": nonce}
    for marker_path in (
        root / ".recipe-importer-browser-fixture.json",
        output / ".fixture-library",
        sources / ".fixture-sources",
    ):
        try:
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise FixtureModeError("Browser fixture marker is missing or invalid.") from exc
        if marker != expected:
            raise FixtureModeError("Browser fixture markers do not match this runtime attestation.")
    return f"{run_id}:{nonce}"


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _production_output_dir() -> Path:
    configured = str(os.environ.get("RECIPE_IMPORTER_PRODUCTION_OUTPUT_DIR", "")).strip()
    return (Path(configured).expanduser() if configured else Path.cwd() / "output").resolve()


def _paths_overlap(first: Path, second: Path) -> bool:
    """Return true when either configured tree contains the other."""
    return _is_relative_to(first, second) or _is_relative_to(second, first)


def _contains_link(path: Path) -> bool:
    """Detect symlinks and Windows junctions in a requested path, including parents."""
    current = path
    while current != current.parent:
        is_junction = getattr(current, "is_junction", None)
        if current.is_symlink() or (callable(is_junction) and is_junction()):
            return True
        current = current.parent
    return False


def require_live_services() -> None:
    """Refuse remote clients in fixture mode, regardless of available secrets."""
    if fixture_mode():
        raise RuntimeError("Live services are disabled in fixture mode. Use an injected fake client.")
