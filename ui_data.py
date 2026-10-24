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
from import_recipes import (
    OCR_STRUCTURE_CACHE_VERSION, OCR_STRUCTURE_PROMPT_VERSION,
    StateStore, _ocr_text_paths, candidate_identity, file_digest,
)
from import_protection import ImportProtectionError, protected_candidate_ids
from quick_edit import ParseWarning, parse_ingredients, parse_instructions
from recipe_quality import ExtractionBatch, RecipeCandidate, is_complete, needs_review
from recipe_validation import validate_recipe
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
    source_candidate: RecipeCandidate | None = None
    approval_method: str = ""
    approval_rationale: str = ""
    no_instructions_reason: str = ""

    @property
    def quality_findings(self):
        from recipe_validation import validate_recipe
        return tuple(validate_recipe(self.candidate))

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


@dataclass(frozen=True)
class SourceTextPage:
    """Read-only, page-bound source transcription and its provenance."""

    text: str = ""
    page_number: int = 1
    page_count: int = 0
    page_numbers: tuple[int, ...] = ()
    status: str = "no_text"
    method: str = ""
    uncertainty: tuple[str, ...] = ()


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
                 verify_sources: bool = True, read_only: bool = False) -> list[RecipeView]:
    """Load local extraction results into stable recipe views.

    Cache files are grouped by source hash and the newest valid version wins.
    Existing state and review records are matched by candidate identity rather
    than the candidate's position in a ``recipes`` array.
    """
    output_dir = Path(output_dir)
    caches = _load_caches(output_dir / "extracted")
    reviews = ReviewStore(output_dir / "review_events.jsonl")
    cached_sources = {source_hash for source_hash, _batch in caches}
    registered_batches: dict[tuple[str, str], list[dict]] = {}
    for event in reviews.events:
        if event.get("event") != "candidate_registration" or not isinstance(event.get("candidate"), dict):
            continue
        key = (str(event.get("source_hash", "")), str(event.get("extraction_id", "")))
        # Reconstruct immutable registrations only when there is no cache for
        # this source at all. A changed cache is a newer extraction; appending
        # the older registered batch beside it would resurrect stale candidates.
        if key[0] and key[1] and key[0] not in cached_sources:
            batch = registered_batches.setdefault(key, [])
            if not any(item.get("candidate_id") == event.get("candidate_id") for item in batch):
                batch.append(event)
    for (source_hash, extraction_id), registrations in registered_batches.items():
        try:
            batch = ExtractionBatch.model_validate({
                "recipes": [item["candidate"] for item in registrations],
            })
            if extraction_identity(source_hash, batch.model_dump(mode="json")) == extraction_id:
                caches.append((source_hash, batch))
        except Exception:
            # The saved registration is useful for audit, but only a verifiable
            # complete extraction batch can reconstruct an import candidate.
            continue
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
        relinked = next((item for item in reversed(reviews.source_relinks.get(identity, []))
                         if item.get("source_hash") == expected_hash
                         and item.get("authority_version") == 1
                         and source_matches(str(item.get("source_path", "")), expected_hash)), None)
        if relinked:
            return str(relinked["source_path"]), True
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
    if read_only:
        registered_batches = []
        for (_source, source_hash, extraction_id, candidates) in registration_batches:
            batch_registrations = []
            for candidate in candidates:
                legacy_id = candidate_identity(source_hash, candidate)
                matches = [registration for events in reviews.registrations.values()
                           for registration in events
                           if registration.get("source_hash") == source_hash
                           and registration.get("extraction_id") == extraction_id
                           and (registration.get("candidate_id") == legacy_id
                                or legacy_id in registration.get("legacy_aliases", ())
                                or registration.get("fingerprint") == legacy_id.rsplit(":", 1)[-1])]
                if len(matches) == 1:
                    batch_registrations.append(matches[0])
                else:
                    batch_registrations.append({
                        "candidate_id": legacy_id, "source_hash": source_hash,
                        "extraction_id": extraction_id, "candidate": candidate.model_dump(mode="json"),
                        "target_bindings": [], "identity_status": "unregistered",
                        "legacy_aliases": [], "source": "",
                    })
            registered_batches.append(batch_registrations)
    else:
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
                candidate_complete=is_complete(reviewed, no_instructions_confirmed=bool(
                    effective and effective.no_instructions_reason)),
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
                identity_status=("registered" if reviews.identity_link_status(
                    identity, source_hash, extraction_id) == "registered"
                    else str(registration.get("identity_status", "registered"))),
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
                source_candidate=RecipeCandidate.model_validate(
                    registration.get("candidate", candidate.model_dump(mode="json"))),
                approval_method=effective.approval_method if effective else "",
                approval_rationale=effective.approval_rationale if effective else "",
                no_instructions_reason=effective.no_instructions_reason if effective else "",
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
        attempt_candidate = next((item for item in load_catalog(attempt_dir, read_only=read_only)
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
            candidate_complete=is_complete(reviewed, no_instructions_confirmed=bool(
                effective.no_instructions_reason)),
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
            identity_status=("registered" if reviews.identity_link_status(
                candidate_id, source_hash, extraction_id) == "registered"
                else str(registration.get("identity_status", "registered"))),
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
            source_candidate=RecipeCandidate.model_validate(
                registration.get("candidate", attempt_candidate.candidate.model_dump(mode="json"))),
            approval_method=effective.approval_method,
            approval_rationale=effective.approval_rationale,
            no_instructions_reason=effective.no_instructions_reason,
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


def extraction_quality_audit(views: Iterable[RecipeView]) -> dict[str, Any]:
    """Build a read-only audit from the supplied current effective projections."""
    rows = []
    inspected = 0
    deferred = 0
    corrected = 0
    for view in views:
        inspected += 1
        findings = list(view.quality_findings)
        if (getattr(view.draft, "repair_proposal", {}) or {}).get("status") == "deferred":
            deferred += 1
        if (view.revision > 0 and view.source_candidate is not None
                and view.source_candidate is not view.candidate
                and validate_recipe(view.source_candidate) and not findings):
            corrected += 1
        if findings:
            rows.append({"view": view, "findings": findings})
    high_priority = sum(1 for row in rows for item in row["findings"]
                        if item.severity == "critical")
    return {
        "rows": rows,
        "inspected": inspected,
        "with_issues": len(rows),
        "high_priority": high_priority,
        "needs_manual_review": sum(not row["view"].protected for row in rows),
        "deferred": deferred,
        "corrected": corrected,
    }


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


_SOURCE_TEXT_CACHE_LIMIT = 4 * 1024 * 1024
_SOURCE_TEXT_METADATA_LIMIT = 1024 * 1024
_SOURCE_TEXT_PAGE_MARKER = re.compile(r"(?m)^---[ \t]+(?:OCR|PDF)[ \t]+PAGE[ \t]+(\d+)[ \t]+---[ \t]*\r?$")


def source_text_for_page(view: RecipeView, page_number: int,
                         output_dir: Path) -> SourceTextPage:
    """Return selectable page text or an existing hash-bound OCR page, without writing."""
    digest = str(view.source_hash or "").casefold()
    if (not view.candidate_id or not re.fullmatch(r"[0-9a-f]{64}", digest)
            or page_number < 1):
        return SourceTextPage(page_number=page_number, status="identity_invalid",
                              method="Source identity unavailable")

    source = Path(view.source_path) if view.source_path else None
    source_exists = False
    source_verified = False
    page_count = 0
    if source is not None:
        from runtime_safety import fixture_mode, fixture_source_allowed
        if fixture_mode() and not fixture_source_allowed(source):
            return SourceTextPage(page_number=page_number, status="source_unavailable",
                                  method="Source text unavailable")
        try:
            source_exists = source.is_file()
        except OSError:
            source_exists = False
        if source_exists:
            if view.source_verified and view.source_available:
                source_verified = True
            else:
                try:
                    source_verified = file_digest(source) == digest
                except OSError:
                    source_verified = False
            if not source_verified:
                return SourceTextPage(page_number=page_number, status="source_mismatch",
                                      method="Source hash does not match this recipe")

    if source_verified and source and source.suffix.casefold() == ".pdf":
        try:
            text, page_count = _selectable_pdf_page_text(
                str(source.resolve()), digest, source.stat().st_mtime_ns,
                source.stat().st_size, page_number,
            )
        except Exception:
            text = ""
        if page_count and page_number > page_count:
            return SourceTextPage(page_number=page_number, page_count=page_count,
                                  page_numbers=tuple(range(1, page_count + 1)),
                                  status="page_unavailable", method="PDF page unavailable")
        if text:
            return SourceTextPage(
                text=text, page_number=page_number, page_count=page_count,
                page_numbers=tuple(range(1, page_count + 1)), status="available",
                method="Selectable PDF text",
                uncertainty=("Selectable text may omit words contained in images or graphics.",),
            )

    output = Path(output_dir)
    paths = _ocr_text_paths(output, digest)
    metadata = _source_text_ocr_metadata(output, digest)
    preferred = metadata[0]
    choices = ["selected"]
    if preferred in {"original", "layout"}:
        choices.append(preferred)
    choices.extend(("original", "layout"))
    cache_path = next((paths[name] for name in dict.fromkeys(choices)
                       if paths[name].is_file()), None)
    method_suffix = ""
    if cache_path is None:
        legacy_path = output / "ocr_text" / f"{digest}.txt"
        if legacy_path.is_file():
            cache_path = legacy_path
            method_suffix = "legacy cache"
    if cache_path is None:
        return SourceTextPage(page_number=page_number, page_count=page_count,
                              page_numbers=tuple(range(1, page_count + 1)),
                              status="no_text", method="No selectable or cached source text")

    try:
        stat = cache_path.stat()
        cache_text, read_status = _read_source_text_cache(
            str(cache_path.resolve()), stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size,
        )
    except OSError:
        cache_text, read_status = "", "cache_unreadable"
    if read_status != "available":
        return SourceTextPage(page_number=page_number, page_count=page_count,
                              status=read_status, method="Cached OCR text unavailable")

    markers = _source_text_page_markers(
        str(cache_path.resolve()), stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size,
    )
    uncertainty = ["Local OCR may misread characters or reading order; compare this text with the scan."]
    if metadata[1]:
        uncertainty.extend(metadata[1])
    if not source_exists:
        uncertainty.insert(0, "Original source is unavailable; this cached text cannot be checked against the scan.")

    if markers:
        page_numbers = tuple(sorted({item[0] for item in markers}))
        matching = [item for item in markers if item[0] == page_number]
        if len(matching) != 1:
            status = "ambiguous_page" if len(matching) > 1 else "no_text"
            return SourceTextPage(page_number=page_number,
                                  page_count=max(page_numbers, default=page_count),
                                  page_numbers=page_numbers, status=status,
                                  method="Cached OCR text with page markers",
                                  uncertainty=tuple(uncertainty))
        _marker_page, marker_start, marker_end = matching[0]
        end = next((item[1] for item in markers if item[1] > marker_start), len(cache_text))
        text = cache_text[marker_end:end].strip()
        page_count = max(page_numbers, default=page_count)
    else:
        page_numbers = ()
        is_single_image = bool(source and source.suffix.casefold() in {".jpg", ".jpeg", ".png", ".webp"})
        is_verified_single_pdf = bool(source_verified and source and source.suffix.casefold() == ".pdf"
                                      and page_count == 1)
        if not (is_single_image or is_verified_single_pdf):
            return SourceTextPage(page_number=page_number, page_count=page_count,
                                  status="ambiguous_page", method="Cached OCR page mapping unavailable",
                                  uncertainty=tuple(uncertainty))
        page_numbers = (1,)
        text = cache_text.strip()
        page_count = 1

    if not text:
        return SourceTextPage(page_number=page_number, page_count=page_count,
                              page_numbers=page_numbers, status="no_text",
                              method="No OCR text for this source page",
                              uncertainty=tuple(uncertainty))

    selected_cache = cache_path == paths["selected"]
    variant_cache = next((name for name in ("original", "layout")
                          if cache_path == paths[name]), "")
    if method_suffix:
        method = "Legacy cached OCR text (variant not recorded)"
    elif selected_cache and preferred:
        method = f"Cached OCR text ({preferred} variant selected)"
    elif selected_cache:
        method = "Cached OCR text (selected variant details not recorded)"
    elif variant_cache in {"original", "layout"}:
        method = f"Cached OCR text ({variant_cache} variant; selected cache unavailable)"
    else:
        method = "Cached OCR text (variant not recorded)"
    return SourceTextPage(text=text, page_number=page_number, page_count=page_count,
                          page_numbers=page_numbers, status="available", method=method,
                          uncertainty=tuple(dict.fromkeys(uncertainty)))


@lru_cache(maxsize=64)
def _selectable_pdf_page_text(path_value: str, digest: str, modified_ns: int,
                              size: int, page_number: int) -> tuple[str, int]:
    """Cache one verified page extraction; callers freshly verify source bytes first."""
    import fitz

    document = fitz.open(path_value)
    try:
        page_count = len(document)
        if not 1 <= page_number <= page_count:
            return "", page_count
        return str(document[page_number - 1].get_text("text") or "").strip(), page_count
    finally:
        document.close()


@lru_cache(maxsize=16)
def _read_source_text_cache(path_value: str, modified_ns: int, changed_ns: int,
                            size: int) -> tuple[str, str]:
    if size > _SOURCE_TEXT_CACHE_LIMIT:
        return "", "cache_unreadable"
    try:
        with Path(path_value).open("rb") as handle:
            raw = handle.read(_SOURCE_TEXT_CACHE_LIMIT + 1)
    except OSError:
        return "", "cache_unreadable"
    if len(raw) > _SOURCE_TEXT_CACHE_LIMIT:
        return "", "cache_unreadable"
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return "", "cache_unreadable"
    return (text, "available") if text.strip() else ("", "no_text")


def _source_text_ocr_metadata(output_dir: Path, digest: str) -> tuple[str, tuple[str, ...]]:
    directory = Path(output_dir) / "extracted"
    try:
        paths = sorted(directory.glob(f"{digest}--ocr-structure--*.json"),
                       key=lambda item: item.stat().st_mtime_ns, reverse=True)[:8]
    except OSError:
        return "", ()
    for path in paths:
        try:
            stat = path.stat()
            variant, notes = _read_source_text_metadata(
                str(path.resolve()), digest, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size,
            )
        except OSError:
            continue
        if variant:
            return variant, notes
    return "", ()


@lru_cache(maxsize=64)
def _read_source_text_metadata(path_value: str, digest: str,
                               modified_ns: int, changed_ns: int,
                               size: int) -> tuple[str, tuple[str, ...]]:
    if size > _SOURCE_TEXT_METADATA_LIMIT:
        return "", ()
    try:
        with Path(path_value).open("rb") as handle:
            raw_bytes = handle.read(_SOURCE_TEXT_METADATA_LIMIT + 1)
        if len(raw_bytes) > _SOURCE_TEXT_METADATA_LIMIT:
            return "", ()
        raw = json.loads(raw_bytes.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return "", ()
    if (not isinstance(raw, dict) or raw.get("source_hash") != digest
            or raw.get("schema_version") != OCR_STRUCTURE_CACHE_VERSION
            or raw.get("prompt_version") != OCR_STRUCTURE_PROMPT_VERSION):
        return "", ()
    variant = str(raw.get("ocr_selected_variant") or "")
    if variant not in {"original", "layout"}:
        variant = ""
    notes = raw.get("ocr_selection_reason", [])
    if not isinstance(notes, list):
        notes = []
    return variant, tuple(str(note)[:300] for note in notes if str(note).strip())[:5]


@lru_cache(maxsize=16)
def _source_text_page_markers(path_value: str, modified_ns: int, changed_ns: int,
                              size: int) -> tuple[tuple[int, int, int], ...]:
    text, status = _read_source_text_cache(path_value, modified_ns, changed_ns, size)
    if status != "available":
        return ()
    return tuple((int(match.group(1)), match.start(), match.end())
                 for match in _SOURCE_TEXT_PAGE_MARKER.finditer(text))


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
