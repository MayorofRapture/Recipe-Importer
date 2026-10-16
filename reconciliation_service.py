"""Isolated intentional re-extraction and explicit candidate reconciliation."""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from batch_store import BatchStore
from import_protection import ImportProtectionError, protected_candidate_ids
from recipe_quality import ExtractionBatch, RecipeCandidate
from review_store import ReviewConflict, ReviewStore, _state_lock
from ui_data import RecipeView, load_catalog
from ui_job_service import GEMINI, LOCAL_OCR, OCR_ASSISTED, JobService


class ReconciliationError(RuntimeError):
    """Retry/mapping input is stale, ambiguous, or protected."""


_VALID_MODES = {LOCAL_OCR, GEMINI, OCR_ASSISTED}
_SECTIONS = {"name", "ingredients", "instructions", "yield_text", "description"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _fingerprint(candidate: RecipeCandidate) -> str:
    return hashlib.sha256(_json(candidate.model_dump(mode="json")).encode("utf-8")).hexdigest()


def _recipe_content_fingerprint(candidate: RecipeCandidate) -> str:
    """Compare recipe content without letting a changed title create a new identity."""
    data = candidate.model_dump(mode="json")
    return hashlib.sha256(_json({
        key: data.get(key) for key in (
            "ingredients", "instructions", "yield_text", "description",
        )
    }).encode("utf-8")).hexdigest()


class RetryService:
    """R5 retry boundary; attempt artifacts never enter the active extraction cache."""

    def __init__(self, output_dir: Path, source_root: Path | None = None,
                 extractor=None, *, config: dict | None = None):
        self.output_dir = Path(output_dir)
        self.source_root = Path(source_root) if source_root is not None else None
        self.extractor = extractor
        self.config = dict(config) if config is not None else None
        self.retry_root = self.output_dir / "reextraction_attempts"
        self.events_path = self.retry_root / "events.jsonl"
        self.review_path = self.output_dir / "review_events.jsonl"

    def preview(self, source_hash: str) -> dict:
        views = [view for view in load_catalog(self.output_dir, self.source_root)
                 if view.source_hash == str(source_hash)]
        if not views:
            raise ReconciliationError("No active extraction matches this source.")
        try:
            protected_ids = protected_candidate_ids(self.output_dir)
        except ImportProtectionError as exc:
            raise ReconciliationError(str(exc)) from exc
        unresolved, protected = [], []
        for view in views:
            row = self._view_summary(view)
            is_protected = view.protected or view.candidate_id in protected_ids
            if is_protected:
                row["protected"] = True
                protected.append(row)
            elif view.status != "Ready to import":
                unresolved.append(row)
        attempts = [item for item in self.attempts()
                    if item.get("active_source_hash") == str(source_hash)]
        source_path = next((view.source_path for view in views if view.source_path), "")
        if not source_path:
            source_path = self._registered_source_path(views)
        changed = bool(source_path and Path(source_path).is_file()
                       and _file_digest(Path(source_path)) != str(source_hash))
        return {
            "source_hash": str(source_hash),
            "source_path": source_path,
            "source_changed": changed,
            "unresolved": unresolved,
            "protected_siblings": protected,
            "previous_attempt": attempts[0] if attempts else None,
            "mode_notice": "The selected source is processed as a whole; only explicitly mapped candidates affect drafts.",
        }

    def start_attempt(self, source_hash: str, mode: str = LOCAL_OCR,
                      cloud_consent: bool = False) -> dict:
        mode = str(mode or "").strip().lower()
        if mode not in _VALID_MODES:
            raise ValueError("Unsupported extraction mode.")
        if mode in {GEMINI, OCR_ASSISTED} and not cloud_consent:
            raise ValueError("Cloud extraction requires explicit consent.")
        plan = self.preview(source_hash)
        if not plan["unresolved"]:
            raise ReconciliationError("There are no unresolved candidates eligible for another extraction.")
        source_path = Path(plan["source_path"])
        try:
            source_bytes = source_path.read_bytes()
        except OSError as exc:
            raise ReconciliationError("The original source is unavailable; no attempt was started.") from exc
        attempt_id = str(uuid.uuid4())
        attempt_dir = self.retry_root / attempt_id
        attempt_dir.mkdir(parents=True, exist_ok=False)
        self._append({
            "event": "attempt_created", "attempt_id": attempt_id,
            "active_source_hash": str(source_hash), "source_path": str(source_path),
            "mode": mode, "cloud_consent": bool(cloud_consent),
            "created_at": _now(), "attempt_dir": str(attempt_dir), "status": "staging",
        })
        try:
            batches = BatchStore(attempt_dir)
            batch = batches.create(f"Retry {source_path.name}")
            staged = batches.stage(batch["batch_id"], source_path.name, source_bytes)
            if staged.get("status") != "staged":
                raise ReconciliationError("The source could not be staged for a separate retry attempt.")
            jobs = JobService(attempt_dir, config=self.config, extractor=self.extractor)
            job = jobs.start(batch["batch_id"], mode, cloud_consent=cloud_consent)
            self._append({
                "event": "attempt_job_started", "attempt_id": attempt_id,
                "batch_id": batch["batch_id"], "source_id": staged["source_id"],
                "attempt_source_hash": staged["digest"], "job_id": job["job_id"],
                "status": job["status"], "updated_at": _now(),
            })
            if self.extractor is not None:
                job = jobs.run_pending(job["job_id"])
                self._append({"event": "attempt_job_updated", "attempt_id": attempt_id,
                              "status": job.get("status", "unknown"), "updated_at": _now()})
        except Exception as exc:
            self._append({"event": "attempt_failed", "attempt_id": attempt_id,
                          "failure_category": type(exc).__name__, "status": "failed",
                          "updated_at": _now()})
            if isinstance(exc, (ValueError, ReconciliationError)):
                raise
            raise ReconciliationError("The retry attempt could not be started; existing review data is unchanged.") from exc
        return self.get_attempt(attempt_id) or {}

    def attempts(self) -> list[dict]:
        latest: dict[str, dict] = {}
        for event in self._read_events():
            attempt_id = str(event.get("attempt_id", ""))
            if attempt_id:
                latest.setdefault(attempt_id, {}).update(event)
        rows = [self._snapshot(item) for item in latest.values()]
        return sorted(rows, key=lambda item: item.get("created_at", ""), reverse=True)

    def get_attempt(self, attempt_id: str) -> dict | None:
        if not self._valid_id(attempt_id):
            return None
        matches = [item for item in self.attempts() if item.get("attempt_id") == attempt_id]
        return matches[0] if matches else None

    def compare(self, attempt_id: str) -> dict:
        attempt = self.get_attempt(attempt_id)
        if not attempt:
            raise ReconciliationError("Retry attempt is unavailable.")
        if attempt.get("status") == "cancelled":
            raise ReconciliationError("This retry attempt was cancelled.")
        if attempt.get("job_status") not in {"completed", "completed_with_errors"}:
            raise ReconciliationError("Retry extraction is not complete yet.")
        old_views = [view for view in load_catalog(self.output_dir, self.source_root)
                     if view.source_hash == attempt.get("active_source_hash")]
        try:
            protected_ids = protected_candidate_ids(self.output_dir)
        except ImportProtectionError as exc:
            raise ReconciliationError(str(exc)) from exc
        new_views = load_catalog(Path(attempt["attempt_dir"]))
        if not new_views:
            raise ReconciliationError("The retry produced no inspectable candidate; prior extraction remains available.")
        old_fingerprints: dict[str, list[str]] = {}
        old_titles: dict[str, list[str]] = {}
        for view in old_views:
            old_fingerprints.setdefault(_fingerprint(view.candidate), []).append(view.candidate_id)
            old_titles.setdefault(view.candidate.name.strip().casefold(), []).append(view.candidate_id)
        new_title_counts: dict[str, int] = {}
        for view in new_views:
            key = view.candidate.name.strip().casefold()
            new_title_counts[key] = new_title_counts.get(key, 0) + 1
        new_candidates = []
        for view in new_views:
            title_ids = old_titles.get(view.candidate.name.strip().casefold(), [])
            matches = old_fingerprints.get(_fingerprint(view.candidate), [])
            unique_match = matches[0] if len(matches) == 1 else None
            new_candidates.append({
                **self._view_summary(view),
                "candidate": view.candidate.model_dump(mode="json"),
                "title_match_ids": title_ids,
                "suggested_match_id": unique_match,
                "match_status": ("unique_content_match" if unique_match else
                                 "ambiguous_title" if title_ids or new_title_counts[view.candidate.name.strip().casefold()] > 1
                                 else "unmatched"),
            })
        return {
            "attempt_id": attempt_id, "active_source_hash": attempt.get("active_source_hash"),
            "attempt_source_hash": attempt.get("attempt_source_hash", ""),
            "source_changed": bool(attempt.get("attempt_source_hash")
                                   and attempt.get("attempt_source_hash") != attempt.get("active_source_hash")),
            "old_candidates": [self._view_summary(view) | {
                "candidate": view.candidate.model_dump(mode="json"),
                "protected": view.protected or view.candidate_id in protected_ids,
            } for view in old_views],
            "new_candidates": new_candidates,
            "mappings_applied": False,
        }

    def apply_mapping(self, attempt_id: str, new_candidate_id: str,
                      target_candidate_id: str | None, sections: list[str],
                      expected_revision: int, operation_id: str, evidence: str) -> dict:
        attempt = self.get_attempt(attempt_id)
        if not attempt or attempt.get("status") == "cancelled":
            raise ReconciliationError("Retry attempt is unavailable or cancelled.")
        comparison = self.compare(attempt_id)
        new = next((item for item in comparison["new_candidates"]
                    if item["candidate_id"] == str(new_candidate_id)), None)
        if not new or new.get("identity_status") != "registered":
            raise ReconciliationError("The selected retry candidate has an ambiguous identity and remains held.")
        selected = list(dict.fromkeys(str(field) for field in sections))
        if not selected or any(field not in _SECTIONS for field in selected):
            raise ValueError("Choose one or more supported recipe sections.")
        if not str(operation_id).strip() or not str(evidence).strip():
            raise ValueError("An operation ID and concise evidence are required for reconciliation.")
        new_recipe = RecipeCandidate.model_validate(new["candidate"])
        if target_candidate_id is None:
            title = new_recipe.name.strip().casefold()
            duplicate_content = any(
                _recipe_content_fingerprint(RecipeCandidate.model_validate(row["candidate"]))
                == _recipe_content_fingerprint(new_recipe)
                for row in comparison["old_candidates"]
            )
            if duplicate_content or any(row["candidate"]["name"].strip().casefold() == title
                                        for row in comparison["old_candidates"]):
                raise ReconciliationError(
                    "This recipe matches existing source content; map it explicitly or defer it."
                )
            if any(row.get("protected") for row in comparison["old_candidates"]):
                raise ReconciliationError(
                    "This source has a protected sibling; an unmatched recipe cannot be declared new "
                    "without an independent source anchor. Map it explicitly or defer it."
                )
            source_path = Path(str(attempt.get("source_path", "")))
            if (not source_path.is_file()
                    or _file_digest(source_path) != str(attempt.get("attempt_source_hash", ""))):
                raise ReconciliationError("The source changed after extraction; restart the comparison before declaring a new recipe.")
            attempt_reviews = ReviewStore(Path(attempt["attempt_dir"]) / "review_events.jsonl")
            attempt_registration = next((item for item in attempt_reviews.registrations.get(new["candidate_id"], [])
                                         if item.get("source_hash") == new["source_hash"]
                                         and item.get("extraction_id") == new["extraction_id"]), None)
            if not attempt_registration:
                raise ReconciliationError("Retry candidate registration is unavailable; it remains held.")
            active_reviews = ReviewStore(self.review_path)
            try:
                active_reviews.register_reconciled_candidate(
                    candidate_id=new["candidate_id"], source_hash=new["source_hash"],
                    extraction_id=new["extraction_id"], source=str(source_path),
                    candidate=new_recipe,
                    target_bindings=attempt_registration.get("target_bindings", []),
                    attempt_id=attempt_id, origin_candidate_id=new["candidate_id"],
                )
            except ReviewConflict as exc:
                raise ReconciliationError(str(exc)) from exc
            event = ReviewStore(self.review_path).record_reconciliation(
                candidate_id=new["candidate_id"], source_hash=new["source_hash"],
                extraction_id=new["extraction_id"], attempt_id=attempt_id,
                new_candidate_id=new["candidate_id"], new_source_hash=new["source_hash"],
                new_extraction_id=new["extraction_id"], target_candidate_id=None,
                decision="declared_new", sections=selected, evidence=str(evidence),
                revision=0, operation_id=operation_id,
            )
            return {"decision": "declared_new", "candidate_id": new["candidate_id"],
                    "event_id": event["event_id"], "active_projection": "available_for_review"}

        target = next((view for view in load_catalog(self.output_dir, self.source_root)
                       if view.candidate_id == str(target_candidate_id)), None)
        if not target or target.source_hash != attempt.get("active_source_hash"):
            raise ReconciliationError("The selected existing candidate is not part of this source.")
        try:
            protected = protected_candidate_ids(self.output_dir)
        except ImportProtectionError as exc:
            raise ReconciliationError(str(exc)) from exc
        if target.protected or target.candidate_id in protected:
            raise ReconciliationError("Imported or uncertain candidates cannot be remapped or bypassed.")
        if target.revision != int(expected_revision):
            raise ReconciliationError("The draft changed after comparison; reload and compare again.")
        if comparison["source_changed"]:
            raise ReconciliationError("Source scan bytes changed after the active extraction. Keep this mapping held until the changed source lineage is explicitly reviewed.")
        recipe = RecipeCandidate.model_validate(target.candidate.model_dump(mode="json"))
        for field in selected:
            setattr(recipe, field, getattr(new_recipe, field))
        base_bindings = list(target.revision_target_bindings or target.target_bindings)
        bindings = [item for item in base_bindings
                    if not (item.get("field") in selected
                            or (item.get("field_path_at_extraction", "").split("[", 1)[0] in selected))]
        reviews = ReviewStore(self.review_path)
        try:
            revision_event = reviews.save_revision(
                candidate_id=target.candidate_id, source_hash=target.source_hash,
                extraction_id=target.extraction_id, expected_parent=target.revision,
                operation_id=operation_id, recipe=recipe.model_dump(mode="json"),
                target_bindings=bindings,
                warnings=[*target.warnings, "Updated from an explicitly mapped retry extraction; approval is required again."],
            )
            lineage = ReviewStore(self.review_path).record_reconciliation(
                candidate_id=target.candidate_id, source_hash=target.source_hash,
                extraction_id=target.extraction_id, attempt_id=attempt_id,
                new_candidate_id=new["candidate_id"], new_source_hash=new["source_hash"],
                new_extraction_id=new["extraction_id"], target_candidate_id=target.candidate_id,
                decision="mapped", sections=selected, evidence=str(evidence),
                revision=int(revision_event["revision"]), operation_id=f"{operation_id}:lineage",
            )
        except ReviewConflict as exc:
            raise ReconciliationError(str(exc)) from exc
        return {"decision": "mapped", "candidate_id": target.candidate_id,
                "revision": revision_event["revision"], "event_id": lineage["event_id"],
                "selected_sections": selected, "approved": False}

    def defer(self, attempt_id: str, new_candidate_id: str,
              operation_id: str, evidence: str) -> dict:
        attempt = self.get_attempt(attempt_id)
        if not attempt or attempt.get("status") == "cancelled":
            raise ReconciliationError("Retry attempt is unavailable or cancelled.")
        new = next((item for item in self.compare(attempt_id)["new_candidates"]
                    if item["candidate_id"] == str(new_candidate_id)), None)
        if not new:
            raise ReconciliationError("The selected retry candidate is unavailable.")
        event = ReviewStore(self.review_path).record_reconciliation(
            candidate_id=new["candidate_id"], source_hash=new["source_hash"],
            extraction_id=new["extraction_id"], attempt_id=attempt_id,
            new_candidate_id=new["candidate_id"], new_source_hash=new["source_hash"],
            new_extraction_id=new["extraction_id"], target_candidate_id=None,
            decision="deferred", sections=[], evidence=evidence,
            revision=0, operation_id=operation_id,
        )
        return {"decision": "deferred", "candidate_id": new["candidate_id"],
                "event_id": event["event_id"], "active_mutation": False}

    def cancel(self, attempt_id: str) -> dict:
        attempt = self.get_attempt(attempt_id)
        if not attempt:
            raise ReconciliationError("Retry attempt is unavailable.")
        if attempt.get("job_id") and attempt.get("job_status") in {
            "queued", "waiting_for_worker", "running", "pausing",
        }:
            JobService(Path(attempt["attempt_dir"]), extractor=self.extractor).pause(attempt["job_id"])
        self._append({"event": "attempt_cancelled", "attempt_id": attempt_id,
                      "status": "cancelled", "updated_at": _now()})
        return self.get_attempt(attempt_id) or {}

    def _snapshot(self, item: dict) -> dict:
        row = dict(item)
        if row.get("job_id") and row.get("attempt_dir"):
            try:
                job = JobService(Path(row["attempt_dir"]), extractor=self.extractor).snapshot(row["job_id"])
            except Exception:
                job = None
            if job:
                row["job_status"] = job.get("status", "unknown")
                row["candidate_count"] = job.get("candidate_count", 0)
                row["failure_count"] = job.get("failure_count", 0)
                if row.get("status") not in {"cancelled", "failed"}:
                    row["status"] = job.get("status", row.get("status", "unknown"))
        return row

    def _append(self, event: dict) -> None:
        with _state_lock(self.review_path):
            events = self._read_events()
            operation_id = str(event.get("attempt_id", "")) + ":" + str(event.get("event", ""))
            event = {"schema_version": 1, "event_id": str(uuid.uuid4()),
                     "operation_id": operation_id, **event}
            prior = next((item for item in events if item.get("operation_id") == operation_id), None)
            if prior:
                ignored = {"event_id", "timestamp", "actor", "updated_at"}
                if ({key: value for key, value in prior.items() if key not in ignored}
                        != {key: value for key, value in event.items() if key not in ignored}):
                    raise ReconciliationError("Retry operation ID was reused for different attempt data.")
                return
            self.retry_root.mkdir(parents=True, exist_ok=True)
            with self.events_path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(_json(event) + "\n")
                handle.flush()
                os.fsync(handle.fileno())

    def _read_events(self) -> list[dict]:
        if not self.events_path.exists():
            return []
        try:
            rows = [json.loads(line) for line in self.events_path.read_text(encoding="utf-8").splitlines()
                    if line.strip()]
        except (OSError, json.JSONDecodeError) as exc:
            raise ReconciliationError("Retry history is unreadable; existing extraction data was preserved.") from exc
        if any(not isinstance(item, dict) for item in rows):
            raise ReconciliationError("Retry history has an unsupported record; existing data was preserved.")
        return rows

    def _registered_source_path(self, views: list[RecipeView]) -> str:
        review = ReviewStore(self.review_path)
        for view in views:
            registrations = review.registrations.get(view.candidate_id, [])
            registration = next((item for item in registrations
                                 if item.get("source_hash") == view.source_hash), None)
            value = str((registration or {}).get("source", ""))
            if not value:
                continue
            original = Path(value)
            candidates = [original if original.is_absolute() else
                          ((self.source_root / original) if self.source_root else original)]
            if self.source_root:
                candidates.append(self.source_root / original.name)
            for candidate in candidates:
                try:
                    if candidate.is_file():
                        return str(candidate)
                except OSError:
                    continue
        return ""

    @staticmethod
    def _valid_id(value: str) -> bool:
        try:
            return str(uuid.UUID(str(value))) == str(value)
        except (ValueError, TypeError, AttributeError):
            return False

    @staticmethod
    def _view_summary(view: RecipeView) -> dict:
        return {"candidate_id": view.candidate_id, "title": view.candidate.name,
                "status": view.status, "revision": view.revision,
                "source_hash": view.source_hash, "extraction_id": view.extraction_id,
                "identity_status": view.identity_status, "protected": view.protected}


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
