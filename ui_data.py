"""Read-only projections for the Streamlit recipe review workspace.

This module deliberately does not call Gemini or Mealie.  It combines the
existing local cache, review ledger, draft ledger, and import state into a
stable view model for the UI.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from draft_store import DraftStore
from import_recipes import StateStore, candidate_identity
from quick_edit import ParseWarning, parse_ingredients, parse_instructions
from recipe_quality import ExtractionBatch, RecipeCandidate, is_complete, needs_review
from review_store import ReviewStore, apply_candidate_decision, apply_review_decision
from ui_contracts import normalize_status


@dataclass(frozen=True)
class RecipeView:
    """Stable, UI-facing projection of one extracted recipe candidate."""

    candidate_id: str
    source_hash: str
    source_path: str
    candidate: RecipeCandidate
    status: str
    protected: bool = False
    import_record: dict[str, Any] = field(default_factory=dict)
    draft: Any | None = None
    warnings: tuple[str, ...] = ()
    import_ready: bool = False

    @property
    def pending_suggestions(self) -> list:
        return [item for item in self.candidate.suggestions if item.review_status == "pending"]

    @property
    def critical_findings(self) -> list:
        return [item for item in self.candidate.missing_information if item.severity == "critical"]


@dataclass(frozen=True)
class SourcePage:
    """A source preview result.  ``image`` is a PIL image when available."""

    image: Any | None
    page_count: int
    page_number: int
    message: str = ""


def load_catalog(output_dir: Path, source_root: Path | None = None) -> list[RecipeView]:
    """Load local extraction results into stable recipe views.

    Cache files are grouped by source hash and the newest valid version wins.
    Existing state and review records are matched by candidate identity rather
    than the candidate's position in a ``recipes`` array.
    """
    output_dir = Path(output_dir)
    caches = _load_caches(output_dir / "extracted")
    reviews = ReviewStore(output_dir / "review_events.jsonl")
    state = StateStore(output_dir / "import_state.jsonl")
    drafts = DraftStore(output_dir / "ui_drafts.jsonl").all_latest()
    source_by_identity = _source_paths(reviews, state)
    views: list[RecipeView] = []

    for source_hash, batch in caches:
        for candidate in batch.recipes:
            identity = candidate_identity(source_hash, candidate)
            reviewed = apply_review_decision(candidate, reviews.decisions_for(identity))
            reviewed = apply_candidate_decision(reviewed, reviews.candidate_decision(identity))
            draft = drafts.get(identity)
            if draft is not None and isinstance(draft.recipe, dict):
                try:
                    reviewed = RecipeCandidate.model_validate(draft.recipe)
                except Exception:
                    # A damaged draft must never hide the source-derived cache.
                    draft = None

            record = _state_record(state, identity, source_hash, reviewed)
            source_path = _source_for(identity, record, source_by_identity, source_root)
            warnings = _warnings(reviewed, draft)
            raw_status = str(record.get("status", "")) if record else ""
            approved = bool(
                reviews.candidate_decision(identity)
                and reviews.candidate_decision(identity).status in {"approved", "edited"}
            )
            status = normalize_status(
                raw_status,
                candidate_complete=is_complete(reviewed),
                requires_review=reviewed.requires_review,
                has_pending_suggestions=bool(reviewed.suggestions and any(
                    item.review_status == "pending" for item in reviewed.suggestions
                )),
                has_critical_findings=any(
                    item.severity == "critical" for item in reviewed.missing_information
                ),
                approved=approved,
            )
            protected = status in {"Imported", "Check Mealie"}
            views.append(RecipeView(
                candidate_id=identity,
                source_hash=source_hash,
                source_path=source_path,
                candidate=reviewed,
                status=status,
                protected=protected,
                import_record=dict(record or {}),
                draft=draft,
                warnings=tuple(warnings),
                import_ready=(status == "Ready to import" and not protected),
            ))
    return views


def next_unresolved(
    views: Iterable[RecipeView],
    current_id: str | None,
    *,
    search: str = "",
    statuses: set[str] | None = None,
) -> RecipeView | None:
    """Return the next actionable view without ever selecting imported work."""
    candidates = [view for view in views if not view.protected and view.status != "Imported"]
    if statuses:
        candidates = [view for view in candidates if view.status in statuses]
    needle = search.strip().casefold()
    if needle:
        candidates = [view for view in candidates if needle in view.candidate.name.casefold()]
    if not candidates:
        return None
    if current_id is None:
        return candidates[0]
    for index, view in enumerate(candidates):
        if view.candidate_id == current_id:
            return candidates[index + 1] if index + 1 < len(candidates) else None
    return candidates[0]


def previous_unresolved(views: Iterable[RecipeView], current_id: str | None) -> RecipeView | None:
    """Return the previous actionable view in stable catalog order."""
    candidates = [view for view in views if not view.protected and view.status != "Imported"]
    if not candidates or current_id is None:
        return None
    for index, view in enumerate(candidates):
        if view.candidate_id == current_id and index > 0:
            return candidates[index - 1]
    return None


def render_source_page(path: Path, page_number: int = 1) -> SourcePage:
    """Render one source page without writing to the source file."""
    path = Path(path)
    if not path.exists():
        return SourcePage(None, 0, page_number, "Source file is unavailable.")
    try:
        if path.suffix.casefold() == ".pdf":
            import fitz

            document = fitz.open(path)
            try:
                page_count = len(document)
                if page_number < 1 or page_number > page_count:
                    return SourcePage(None, page_count, page_number, "That source page is unavailable.")
                pixmap = document[page_number - 1].get_pixmap(matrix=fitz.Matrix(1.5, 1.5), alpha=False)
                from PIL import Image

                image = Image.frombytes("RGB", [pixmap.width, pixmap.height], pixmap.samples)
                return SourcePage(image, page_count, page_number)
            finally:
                document.close()
        if path.suffix.casefold() in {".jpg", ".jpeg", ".png", ".webp"}:
            from PIL import Image

            with Image.open(path) as source:
                return SourcePage(source.convert("RGB").copy(), 1, 1)
        return SourcePage(None, 0, page_number, "This source format cannot be previewed yet.")
    except Exception as exc:
        return SourcePage(None, 0, page_number, f"The source preview could not be opened: {exc}")


def _load_caches(directory: Path) -> list[tuple[str, ExtractionBatch]]:
    grouped: dict[str, list[Path]] = {}
    if not directory.exists():
        return []
    for path in directory.glob("*.json"):
        if path.name.endswith(".bak"):
            continue
        digest = path.stem.split("--", 1)[0]
        grouped.setdefault(digest, []).append(path)
    loaded: list[tuple[str, ExtractionBatch]] = []
    for digest, paths in sorted(grouped.items()):
        for path in sorted(paths, key=lambda item: item.stat().st_mtime_ns, reverse=True):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(raw, dict) and "batch" in raw:
                    raw = raw["batch"]
                loaded.append((str(raw.get("source_hash") or digest) if isinstance(raw, dict) else digest,
                               ExtractionBatch.model_validate(raw)))
                break
            except (OSError, ValueError, TypeError):
                continue
    return loaded


def _source_paths(reviews: ReviewStore, state: StateStore) -> dict[str, str]:
    paths: dict[str, str] = {}
    for identity, event in reviews.candidates.items():
        if event.get("source"):
            paths[identity] = str(event["source"])
    for record in state.records.values():
        identity = str(record.get("identity", ""))
        if identity and record.get("file"):
            paths.setdefault(identity, str(record["file"]))
    return paths


def _source_for(identity: str, record: dict[str, Any] | None,
                source_by_identity: dict[str, str], source_root: Path | None) -> str:
    value = source_by_identity.get(identity) or str((record or {}).get("file", ""))
    if value and Path(value).exists():
        return value
    if value and source_root is not None:
        candidate = Path(value)
        if not candidate.is_absolute():
            candidate = source_root / candidate
        if candidate.exists():
            return str(candidate)
    return value


def _state_record(state: StateStore, identity: str, source_hash: str,
                  candidate: RecipeCandidate) -> dict[str, Any] | None:
    for record in state.records.values():
        if record.get("identity") == identity:
            return record
    # Legacy state can still be useful when the title and source hash match.
    for index in range(len(state.records) + 1):
        record = state.find_candidate(source_hash, index, candidate)
        if record:
            return record
    return None


def _warnings(candidate: RecipeCandidate, draft: Any | None) -> list[str]:
    warnings = [str(item) for item in candidate.warnings]
    for finding in candidate.missing_information:
        warnings.append(f"{finding.field_path}: {finding.detail}")
    ingredient_text = draft.raw_ingredients if draft is not None else "\n".join(candidate.ingredients)
    instruction_text = draft.raw_instructions if draft is not None else "\n\n".join(candidate.instructions)
    parsed = [*parse_ingredients(ingredient_text).warnings, *parse_instructions(instruction_text).warnings]
    warnings.extend(_format_parse_warning(item) for item in parsed)
    return list(dict.fromkeys(item for item in warnings if item.strip()))


def _format_parse_warning(warning: ParseWarning) -> str:
    return f"{warning.field_path}: {warning.message}"
