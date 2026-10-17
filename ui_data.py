"""Local review projection for Streamlit and the CLI.

This module does not call Gemini or Mealie. It registers cached candidates
through the review event store, then combines that authority with legacy
drafts and import state into a stable UI view model.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable
from functools import lru_cache

from draft_store import DraftStore
from import_recipes import StateStore, candidate_identity, file_digest
from import_protection import ImportProtectionError, protected_candidate_ids
from quick_edit import ParseWarning, parse_ingredients, parse_instructions
from recipe_quality import ExtractionBatch, RecipeCandidate, is_complete, needs_review
from review_store import ReviewStore, apply_candidate_decision, apply_review_decision, extraction_identity
from ui_contracts import normalize_status
from review_service import project_legacy_review
from batch_store import BatchStore


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
    extraction_id: str = ""
    revision: int = 0
    content_digest: str = ""
    target_bindings: tuple[dict[str, Any], ...] = ()
    identity_status: str = "registered"
    source_available: bool = False
    source_verified: bool = False
    legacy_draft_conflict: bool = False
    revision_target_bindings: tuple[dict[str, Any], ...] = ()
    revision_raw_bulk_text: dict[str, str] = field(default_factory=dict)
    revision_mode: str = "structured"
    source_display_name: str = ""
    batch_name: str = "Existing library"
    batch_tag: str = ""
    latest_suggestion_request: dict[str, Any] | None = None

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


class _SourceIndex:
    """Lazy per-catalog basename index for legacy source-path recovery."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.by_name: dict[str, list[Path]] = {}
        self._built = False

    def find(self, name: str) -> list[Path]:
        if not self._built:
            for path in self.root.rglob("*"):
                try:
                    if path.is_file():
                        self.by_name.setdefault(path.name, []).append(path)
                except OSError:
                    continue
            self._built = True
        return self.by_name.get(name, [])


def load_catalog(output_dir: Path, source_root: Path | None = None, *,
                 verify_sources: bool = True) -> list[RecipeView]:
    """Load local extraction results into stable recipe views.

    Cache files are grouped by source hash and the newest valid version wins.
    Existing state and review records are matched by candidate identity rather
    than the candidate's position in a ``recipes`` array.
    """
    output_dir = Path(output_dir)
    caches = _load_caches(output_dir / "extracted")
    reviews = ReviewStore(output_dir / "review_events.jsonl")
    state = StateStore(output_dir / "import_state.jsonl")
    try:
        protected_ids = protected_candidate_ids(output_dir)
        protection_unreadable = False
    except ImportProtectionError:
        protected_ids = set()
        protection_unreadable = True
    drafts = DraftStore(output_dir / "ui_drafts.jsonl").all_latest()
    source_by_identity = _source_paths(reviews, state)
    source_index = _SourceIndex(source_root) if source_root is not None else None
    batch_metadata = _batch_source_metadata(output_dir)
    source_hashes: dict[str, str | None] = {}

    def source_matches(path: str, expected_hash: str) -> bool:
        if not path or not expected_hash:
            return False
        return _cached_digest(Path(path), source_hashes) == expected_hash

    def source_path_for(identity: str, record: dict[str, Any] | None, expected_hash: str,
                        source_hint: str = "") -> tuple[str, bool]:
        if verify_sources:
            path = _source_for(
                identity, record, source_by_identity, source_root,
                expected_hash, source_hint, source_hashes, source_index,
            )
        else:
            path = _source_for_display(
                identity, record, source_by_identity, source_root, source_hint, source_index,
            )
        if verify_sources:
            return path, source_matches(path, expected_hash)
        try:
            return path, bool(path and Path(path).is_file())
        except OSError:
            return path, False

    # These projections depend only on the already-loaded ledger, not on a cache
    # source; build them once instead of rematerializing them for every source.
    legacy_decisions: dict[str, list] = {}
    legacy_candidate_decisions: dict[str, list] = {}
    for destination, source in ((legacy_decisions, reviews.decisions),
                                (legacy_candidate_decisions, reviews.candidate_decisions)):
        for key, values in source.items():
            canonical_id = reviews.canonical_candidate_id(key)
            destination.setdefault(canonical_id, []).extend(values)
        for values in destination.values():
            values.sort(key=lambda value: value.timestamp)
    views: list[RecipeView] = []

    registration_batches = [
        ("", source_hash, extraction_identity(source_hash, batch.model_dump(mode="json")), batch.recipes)
        for source_hash, batch in caches
    ]
    registered_batches = reviews.register_candidate_batches(registration_batches)

    for (source_hash, batch), registrations in zip(caches, registered_batches):
        extraction_id = extraction_identity(source_hash, batch.model_dump(mode="json"))
        title_counts = {}
        for item in batch.recipes:
            title_counts[item.name.casefold().strip()] = title_counts.get(item.name.casefold().strip(), 0) + 1
        for candidate, registration in zip(batch.recipes, registrations):
            identity = registration["candidate_id"]
            draft = drafts.get(identity) or drafts.get(registration.get("legacy_id", ""))
            if draft is not None and draft.source_hash and draft.source_hash != source_hash:
                draft = None
            legacy_review = project_legacy_review(
                candidate, legacy_decisions.get(identity, []),
                next(iter(reversed(legacy_candidate_decisions.get(identity, []))), None), draft,
            )
            reviewed = legacy_review.recipe

            effective = reviews.project_loaded(identity, source_hash, extraction_id, reviewed, draft)
            if effective.revision:
                try:
                    reviewed = RecipeCandidate.model_validate(effective.recipe)
                except Exception:
                    effective = None

            record = _state_record(state, identity, source_hash, reviewed,
                                   (identity, *tuple(registration.get("legacy_aliases", ()))),
                                   allow_legacy_title_match=title_counts[candidate.name.casefold().strip()] == 1)
            registration_sources = [str(registration.get("source", "")),
                                    *(source_by_identity.get(str(alias), "")
                                      for alias in registration.get("legacy_aliases", []))]
            source_hint = next((item for item in registration_sources if item), "")
            source_path, source_available = source_path_for(
                identity, record, source_hash, source_hint,
            )
            source_meta = batch_metadata.get(source_hash, {})
            if verify_sources:
                if (not source_available
                        and source_matches(str(source_meta.get("staged_path", "")), source_hash)):
                    source_path = str(source_meta["staged_path"])
                    source_available = True
            elif not source_path or not Path(source_path).is_file():
                staged_path = str(source_meta.get("staged_path", ""))
                if staged_path and Path(staged_path).is_file():
                    source_path = staged_path
                    source_available = True
            warnings = _warnings(reviewed, draft)
            raw_status = str(record.get("status", "")) if record else ""
            approved = bool(effective.import_ready if effective and effective.revision
                            else legacy_review.approved)
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
            intent_protected = (protection_unreadable or identity in protected_ids
                                or bool(set(registration.get("legacy_aliases", ())) & protected_ids))
            if intent_protected and status not in {"Imported", "Check Mealie"}:
                status = "Check Mealie"
                if not record:
                    record = {"status": "UNCERTAIN", "creation_status": "uncertain",
                              "title": reviewed.name, "identity": identity,
                              "protection_source": "durable_import_intent"}
            protected = status in {"Imported", "Check Mealie"}
            latest_revision = max(
                (item for item in reviews.revisions.get(identity, [])
                 if item.get("extraction_id") == extraction_id),
                key=lambda item: int(item.get("revision", 0)), default={},
            )
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
                extraction_id=extraction_id,
                revision=effective.revision if effective else 0,
                content_digest=effective.content_sha256 if effective else "",
                target_bindings=tuple(registration.get("target_bindings", [])),
                identity_status=str(registration.get("identity_status", "registered")),
                source_available=source_available,
                source_verified=verify_sources and source_available,
                legacy_draft_conflict=bool(effective and effective.legacy_draft_conflict),
                revision_target_bindings=tuple(latest_revision.get("target_bindings", [])),
                revision_raw_bulk_text=dict(latest_revision.get("raw_bulk_text", {})),
                revision_mode=str(latest_revision.get("mode", "structured")),
                source_display_name=str(source_meta.get("original_filename") or human_source_name(source_path)),
                batch_name=str(source_meta.get("batch_name") or "Existing library"),
                batch_tag=str(source_meta.get("batch_tag", "")),
                latest_suggestion_request=reviews.latest_suggestion_request_loaded(
                    identity, source_hash, extraction_id),
            ))

    # Explicitly declared new retry candidates stay in their attempt cache. They
    # enter the active review projection only after the append-only decision and
    # registration events exist; no sibling cache is copied or replaced.
    visible = {(view.candidate_id, view.source_hash, view.extraction_id) for view in views}
    for lineage in reviews.events:
        if lineage.get("event") != "candidate_reconciliation" or lineage.get("decision") != "declared_new":
            continue
        candidate_id = str(lineage.get("new_candidate_id", ""))
        source_hash = str(lineage.get("new_source_hash", ""))
        extraction_id = str(lineage.get("new_extraction_id", ""))
        key = (candidate_id, source_hash, extraction_id)
        if not candidate_id or key in visible:
            continue
        attempt_dir = output_dir / "reextraction_attempts" / str(lineage.get("attempt_id", ""))
        attempt_candidate = next((item for item in load_catalog(attempt_dir)
                                 if item.candidate_id == candidate_id
                                 and item.source_hash == source_hash
                                 and item.extraction_id == extraction_id), None)
        registration = next((item for item in reviews.registrations.get(candidate_id, [])
                             if item.get("source_hash") == source_hash
                             and item.get("extraction_id") == extraction_id), None)
        if not attempt_candidate or not registration:
            continue
        candidate = RecipeCandidate.model_validate(registration["candidate"])
        draft = drafts.get(candidate_id)
        if draft is not None and draft.source_hash and draft.source_hash != source_hash:
            draft = None
        legacy_review = project_legacy_review(
            candidate, legacy_decisions.get(candidate_id, []),
            next(iter(reversed(legacy_candidate_decisions.get(candidate_id, []))), None), draft,
        )
        effective = reviews.project_loaded(candidate_id, source_hash, extraction_id,
                                           legacy_review.recipe, draft)
        reviewed = RecipeCandidate.model_validate(effective.recipe)
        record = _state_record(state, candidate_id, source_hash, reviewed,
                               tuple(registration.get("legacy_aliases", ())))
        source_path, source_available = source_path_for(candidate_id, record, source_hash)
        source_meta = batch_metadata.get(source_hash, {})
        if verify_sources:
            if (not source_available
                    and source_matches(str(source_meta.get("staged_path", "")), source_hash)):
                source_path = str(source_meta["staged_path"])
                source_available = True
        elif not source_path or not Path(source_path).is_file():
            staged_path = str(source_meta.get("staged_path", ""))
            if staged_path and Path(staged_path).is_file():
                source_path = staged_path
                source_available = True
        approved = bool(effective.import_ready if effective.revision else legacy_review.approved)
        status = normalize_status(
            str((record or {}).get("status", "")),
            candidate_complete=is_complete(reviewed),
            requires_review=reviewed.requires_review,
            has_pending_suggestions=any(item.review_status == "pending" for item in reviewed.suggestions),
            has_critical_findings=any(item.severity == "critical" for item in reviewed.missing_information),
            approved=approved,
        )
        intent_protected = (protection_unreadable or candidate_id in protected_ids
                            or bool(set(registration.get("legacy_aliases", ())) & protected_ids))
        if intent_protected and status not in {"Imported", "Check Mealie"}:
            status = "Check Mealie"
            if not record:
                record = {"status": "UNCERTAIN", "creation_status": "uncertain",
                          "title": reviewed.name, "identity": candidate_id,
                          "protection_source": "durable_import_intent"}
        latest_revision = max(
            (item for item in reviews.revisions.get(candidate_id, [])
             if item.get("source_hash") == source_hash and item.get("extraction_id") == extraction_id),
            key=lambda item: int(item.get("revision", 0)), default={},
        )
        views.append(RecipeView(
            candidate_id=candidate_id, source_hash=source_hash, source_path=source_path,
            candidate=reviewed, status=status,
            protected=status in {"Imported", "Check Mealie"}, import_record=dict(record or {}),
            draft=draft, warnings=tuple(_warnings(reviewed, draft)),
            import_ready=status == "Ready to import" and not intent_protected,
            extraction_id=extraction_id, revision=effective.revision,
            target_bindings=tuple(registration.get("target_bindings", [])),
            identity_status=str(registration.get("identity_status", "registered")),
            source_available=source_available,
            source_verified=verify_sources and source_available,
            legacy_draft_conflict=effective.legacy_draft_conflict,
            revision_target_bindings=tuple(latest_revision.get("target_bindings", [])),
            revision_raw_bulk_text=dict(latest_revision.get("raw_bulk_text", {})),
            revision_mode=str(latest_revision.get("mode", "structured")),
            source_display_name=str(source_meta.get("original_filename") or human_source_name(source_path)),
            batch_name=str(source_meta.get("batch_name") or "Existing library"),
            batch_tag=str(source_meta.get("batch_tag", "")),
            latest_suggestion_request=reviews.latest_suggestion_request_loaded(
                candidate_id, source_hash, extraction_id),
        ))
        visible.add(key)
    return views


def human_source_name(source_path: str | Path) -> str:
    """Return a safe human filename without exposing content-addressed names."""
    name = Path(source_path).name
    if re.fullmatch(r"[0-9a-fA-F]{64}(?:-[0-9a-fA-F-]{36})?\.[A-Za-z0-9]+", name):
        return "Source scan"
    return name or "Source unavailable"


def _batch_source_metadata(output_dir: Path) -> dict[str, dict[str, str]]:
    metadata: dict[str, dict[str, str]] = {}
    for batch in BatchStore(output_dir).list_batches():
        for source in batch.get("sources", []):
            digest = str(source.get("digest", ""))
            if digest:
                metadata[digest] = {
                    "original_filename": str(source.get("original_filename", "")),
                    "staged_path": str(source.get("staged_path", "")),
                    "batch_name": str(batch.get("name", "")),
                    "batch_tag": str(batch.get("tag", "")),
                }
    return metadata


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


def render_source_page(path: Path, page_number: int = 1, zoom: float = 1) -> SourcePage:
    """Render one source page without writing to the source file."""
    path = Path(path)
    if not .5 <= zoom <= 3:
        raise ValueError("Source zoom must be between 0.5 and 3")
    from runtime_safety import fixture_mode, fixture_source_allowed
    if fixture_mode() and not fixture_source_allowed(path):
        return SourcePage(None, 0, page_number,
                          "Source preview is unavailable outside the marked demonstration library.")
    try:
        stamp = path.stat()
    except OSError:
        return SourcePage(None, 0, page_number, "Source file is unavailable.")
    return _render_source_cached(str(path.resolve()), stamp.st_mtime_ns, stamp.st_size,
                                 int(page_number), float(zoom))


def _source_preview_pixel_limit(zoom: float) -> tuple[int, int]:
    """Keep Fit close to display resolution while retaining two inspect tiers."""
    if zoom <= 1:
        return (1200, 1600)
    if zoom <= 1.5:
        return (1800, 2400)
    return (2400, 3200)


@lru_cache(maxsize=4)
def _render_source_cached(path_value: str, modified_ns: int, size: int,
                          page_number: int, zoom: float) -> SourcePage:
    """Bound preview memory to four images; file metadata invalidates old pages."""
    path = Path(path_value)
    try:
        if path.suffix.casefold() == ".pdf":
            import fitz

            document = fitz.open(path)
            try:
                page_count = len(document)
                if page_number < 1 or page_number > page_count:
                    return SourcePage(None, page_count, page_number, "That source page is unavailable.")
                page = document[page_number - 1]
                max_width, max_height = _source_preview_pixel_limit(zoom)
                scale = min(1.5 * zoom, max_width / max(1, page.rect.width),
                            max_height / max(1, page.rect.height))
                pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
                from PIL import Image

                image = Image.frombytes("RGB", [pixmap.width, pixmap.height], pixmap.samples)
                return SourcePage(image, page_count, page_number)
            finally:
                document.close()
        if path.suffix.casefold() in {".jpg", ".jpeg", ".png", ".webp"}:
            from PIL import Image

            with Image.open(path) as source:
                image = source.convert("RGB")
                image.thumbnail(_source_preview_pixel_limit(zoom))
                return SourcePage(image.copy(), 1, 1)
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
                stat = path.stat()
                source_hash, batch = _cached_extraction_batch(
                    str(path), stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size, digest,
                )
                loaded.append((source_hash, batch.model_copy(deep=True)))
                break
            except (OSError, ValueError, TypeError):
                continue
    return loaded


@lru_cache(maxsize=2048)
def _cached_extraction_batch(path: str, modified_ns: int, changed_ns: int, size: int,
                             fallback_hash: str) -> tuple[str, ExtractionBatch]:
    """Reuse display extraction data until an external writer changes the file metadata."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(raw, dict) and "batch" in raw:
        raw = raw["batch"]
    source_hash = str(raw.get("source_hash") or fallback_hash) if isinstance(raw, dict) else fallback_hash
    return source_hash, ExtractionBatch.model_validate(raw)


def _source_paths(reviews: ReviewStore, state: StateStore) -> dict[str, str]:
    paths: dict[str, str] = {}
    for identity, registrations in reviews.registrations.items():
        for registration in registrations:
            source = str(registration.get("source", ""))
            if source:
                paths[identity] = source
                for alias in registration.get("legacy_aliases", []):
                    paths.setdefault(str(alias), source)
    for identity, event in reviews.candidates.items():
        if event.get("source"):
            paths[identity] = str(event["source"])
    for candidate_id, registrations in reviews.registrations.items():
        for registration in registrations:
            aliases = {str(item) for item in registration.get("legacy_aliases", [])}
            source = next((paths[item] for item in aliases if item in paths), "")
            if source:
                paths.setdefault(candidate_id, source)
    for record in state.records.values():
        identity = str(record.get("identity", ""))
        if identity and record.get("file"):
            paths.setdefault(identity, str(record["file"]))
    return paths


def _source_for(identity: str, record: dict[str, Any] | None,
                source_by_identity: dict[str, str], source_root: Path | None,
                expected_hash: str = "", source_hint: str = "",
                source_hashes: dict[str, str | None] | None = None,
                source_index: _SourceIndex | None = None) -> str:
    value = source_hint or source_by_identity.get(identity) or str((record or {}).get("file", ""))
    candidates: list[Path] = []
    if value:
        original = Path(value)
        candidates.append(original if original.is_absolute() else
                          (source_root / original if source_root else original))
    if source_root is not None and value:
        candidates.append(source_root / Path(value).name)
        if source_root.is_dir():
            fallback = (source_index.find(Path(value).name) if source_index is not None
                        else source_root.rglob(Path(value).name))
            candidates.extend(path for path in fallback if path not in candidates)
    for candidate in candidates:
        try:
            if not candidate.is_file():
                continue
            digest = (_cached_digest(candidate, source_hashes) if source_hashes is not None
                      else file_digest(candidate)) if expected_hash else ""
            if not expected_hash or digest == expected_hash:
                return str(candidate)
        except OSError:
            continue
    # Keep an unverified legacy location visible for inspection; source_available
    # remains false and prevents any new approval.
    if not expected_hash and value:
        return value
    return ""


def _source_for_display(identity: str, record: dict[str, Any] | None,
                        source_by_identity: dict[str, str], source_root: Path | None,
                        source_hint: str = "", source_index: _SourceIndex | None = None) -> str:
    """Resolve an exact path cheaply; accept basename recovery only when unique."""
    value = source_hint or source_by_identity.get(identity) or str((record or {}).get("file", ""))
    if not value:
        return ""
    original = Path(value)
    direct = original if original.is_absolute() else (source_root / original if source_root else original)
    try:
        if direct.is_file():
            return str(direct)
    except OSError:
        pass
    if source_root is None or not source_root.is_dir():
        return value
    matches = source_index.find(original.name) if source_index is not None else list(source_root.rglob(original.name))
    existing = []
    for path in matches:
        try:
            if path.is_file():
                existing.append(path)
        except OSError:
            continue
    return str(existing[0]) if len(existing) == 1 else ""


def verify_selected_source(view: RecipeView) -> RecipeView:
    """Freshly verify one selected scan against its extraction before detail use."""
    if not view.source_path or not view.source_hash:
        return replace(view, source_available=False, source_verified=False)
    try:
        matches = file_digest(Path(view.source_path)) == view.source_hash
    except OSError:
        matches = False
    return replace(view, source_available=matches, source_verified=matches)


def _cached_digest(path: Path, digests: dict[str, str | None]) -> str | None:
    key = str(path)
    if key not in digests:
        try:
            stat = path.stat()
            digests[key] = _cached_source_digest(
                key, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size,
            )
        except OSError:
            digests[key] = None
    return digests[key]


@lru_cache(maxsize=4096)
def _cached_source_digest(path: str, modified_ns: int, changed_ns: int, size: int) -> str:
    """Cache catalog-only hash results; approval and import rehash source bytes directly."""
    return file_digest(Path(path))


def _state_record(state: StateStore, identity: str, source_hash: str,
                  candidate: RecipeCandidate, aliases: tuple[str, ...] = (),
                  allow_legacy_title_match: bool = True) -> dict[str, Any] | None:
    try:
        return state.find_candidate(
            source_hash, 0, candidate, tuple(dict.fromkeys((identity, *aliases))),
            allow_legacy_title_match=allow_legacy_title_match,
        )
    except Exception as exc:
        from import_recipes import AmbiguousCandidate
        if isinstance(exc, AmbiguousCandidate):
            return {"status": "UNCERTAIN", "note": str(exc)}
        raise


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
