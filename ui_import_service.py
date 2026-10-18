"""Selected-recipe Mealie import and history operations."""
from __future__ import annotations

import json
import hashlib
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from import_recipes import StateStore, file_digest
from import_protection import (
    ImportProtectionError, candidate_identity_group, protected_candidate_ids_from_records,
    protected_identity_groups, read_import_state,
)
from recipe_quality import RecipeCandidate, is_complete, schema_recipe
from review_store import ReviewConflict, ReviewStore, _recipe_digest, _state_lock
from ui_data import RecipeView, load_catalog


class ImportServiceError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _encoded(event: dict) -> str:
    return json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class ImportService:
    """UI import boundary. It never loads credentials or constructs a client."""

    def __init__(self, output_dir: Path, source_root: Path | None = None, client: Any = None,
                 *, review_path: Path | None = None, state_path: Path | None = None,
                 supports_empty_instructions: bool | None = None):
        self.output_dir = Path(output_dir)
        self.source_root = Path(source_root) if source_root is not None else None
        self.client = client
        configured_empty_instruction_support = (
            str(os.environ.get("MEALIE_SUPPORTS_EMPTY_INSTRUCTIONS", "")).strip().casefold()
            in {"1", "true", "yes", "on"}
        )
        self.supports_empty_instructions = bool(
            getattr(client, "supports_empty_instructions", False)
            if supports_empty_instructions is None
            else supports_empty_instructions
        ) or configured_empty_instruction_support
        self.event_path = self.output_dir / "ui_import_events.jsonl"
        self.review_path = Path(review_path) if review_path is not None else self.output_dir / "review_events.jsonl"
        self.state_path = Path(state_path) if state_path is not None else self.output_dir / "import_state.jsonl"

    def preview(self, candidate_ids: list[str], batch_tag: str = "") -> dict:
        tag = str(batch_tag or "").strip()
        selected = list(dict.fromkeys(str(value) for value in candidate_ids if str(value).strip()))
        views = {view.candidate_id: view for view in load_catalog(self.output_dir, self.source_root)}
        review = ReviewStore(self.review_path, state_path=self.state_path)
        plan_id = str(uuid.uuid4())
        eligible: list[dict] = []
        excluded: list[dict] = []
        with _state_lock(self.review_path):
            review._reload()
            events = self._read_events()
            protection = self._protection_snapshot(review, events)
            for candidate_id in selected:
                view = views.get(candidate_id)
                reason, recipe = self._eligibility(review, events, view, protection)
                if reason:
                    excluded.append({"candidate_id": candidate_id, "reason": reason})
                    continue
                effective = self._effective(review, view)
                eligible.append({
                    "candidate_id": candidate_id, "title": view.candidate.name,
                    "source_path": view.source_path, "source_hash": view.source_hash,
                    "extraction_id": view.extraction_id, "revision": view.revision,
                    "content_sha256": effective.content_sha256, "recipe": recipe,
                    "approval_method": effective.approval_method,
                    "approval_rationale": effective.approval_rationale,
                    "no_instructions_reason": effective.no_instructions_reason,
                })
            self._append_locked({
                "event": "plan_created", "schema_version": 1, "event_id": str(uuid.uuid4()),
                "plan_id": plan_id, "created_at": _now(), "batch_tag": tag,
                "eligible": [{key: row[key] for key in (
                    "candidate_id", "title", "source_path", "source_hash", "extraction_id",
                    "revision", "content_sha256", "approval_method",
                    "approval_rationale", "no_instructions_reason",
                )} for row in eligible],
                "excluded": excluded,
            })
        return {"plan_id": plan_id, "eligible": eligible, "excluded": excluded, "batch_tag": tag}

    def dispatch(self, plan_id: str) -> list[dict]:
        self._require_client()
        plan = self._plan(plan_id)
        if plan is None:
            raise ImportServiceError("Import plan is unavailable; create a new preflight.")
        outcomes = []
        batch_tag = str(plan.get("batch_tag", ""))
        views = {view.candidate_id: view for view in load_catalog(self.output_dir, self.source_root)}
        for planned in plan.get("eligible", []):
            candidate_id = str(planned.get("candidate_id", ""))
            view = views.get(candidate_id)
            review = ReviewStore(self.review_path, state_path=self.state_path)
            attempt_id = str(uuid.uuid4())
            with _state_lock(self.review_path):
                review._reload()
                events = self._read_events()
                try:
                    protection = self._protection_snapshot(review, events)
                except ImportProtectionError:
                    outcomes.append(self._outcome(candidate_id, planned.get("title", ""),
                                                  "uncertain", reason="Import history is unreadable; candidate remains protected."))
                    continue
                reason, recipe = self._eligibility(review, events, view, protection)
                if reason:
                    blocked_status = self._blocked_status(events, candidate_id)
                    outcomes.append(self._outcome(candidate_id, planned.get("title", ""),
                                                  blocked_status or "not_sent", reason=reason))
                    continue
                effective = self._effective(review, view)
                digest = self._digest(review, view)
                if (view.revision != planned.get("revision")
                        or digest != planned.get("content_sha256")
                        or view.source_hash != planned.get("source_hash")
                        or view.extraction_id != planned.get("extraction_id")):
                    outcomes.append(self._outcome(candidate_id, view.candidate.name,
                                                  "not_sent", reason="Approval changed after preflight."))
                    continue
                self._append_locked({
                    "event": "intent_started", "schema_version": 1,
                    "event_id": str(uuid.uuid4()), "attempt_id": attempt_id,
                    "plan_id": plan_id, "candidate_id": candidate_id,
                    "source_hash": view.source_hash, "extraction_id": view.extraction_id,
                    "revision": view.revision, "content_sha256": digest,
                    "title": view.candidate.name, "source_path": view.source_path,
                    "batch_tag": batch_tag, "created_at": _now(),
                    "approval_method": effective.approval_method,
                    "approval_rationale": effective.approval_rationale,
                    "no_instructions_reason": effective.no_instructions_reason,
                    "authorization_mode": "approved_revision",
                    "attempt_evidence_version": 2,
                    "recipe_snapshot": effective.recipe,
                    "outbound_payload": schema_recipe(RecipeCandidate.model_validate(recipe)),
                })
            try:
                slug = self.client.create_recipe(RecipeCandidate.model_validate(recipe))
                if not isinstance(slug, str) or not slug.strip():
                    raise RuntimeError("invalid_create_response")
                slug = slug.strip()
            except Exception as exc:
                status, category, http_status = self._create_failure(exc)
                with _state_lock(self.review_path):
                    self._append_locked({
                        "event": "create_uncertain" if status == "uncertain" else "create_rejected",
                        "schema_version": 1, "event_id": str(uuid.uuid4()),
                        "attempt_id": attempt_id, "candidate_id": candidate_id,
                        "title": planned.get("title", ""), "source_hash": planned.get("source_hash", ""),
                        "extraction_id": view.extraction_id, "revision": view.revision,
                        "content_sha256": digest,
                        "empty_instructions_rejected": (
                            not any(str(item).strip() for item in RecipeCandidate.model_validate(recipe).instructions)
                            and status == "rejected"),
                        "source_path": planned.get("source_path", ""), "batch_tag": batch_tag,
                        "category": category, "http_status": http_status, "created_at": _now(),
                    })
                outcomes.append(self._outcome(candidate_id, planned.get("title", ""), status,
                                              reason=self._failure_message(category, http_status),
                                              error_category=category, http_status=http_status))
                continue
            try:
                StateStore(self.state_path).add(
                    candidate_id, "IMPORTED", slug, view.source_path, view.candidate.name,
                    identity=candidate_id, source_hash=view.source_hash,
                    extraction_id=view.extraction_id, review_revision=view.revision,
                    content_sha256=digest, import_operation_id=plan_id,
                    creation_status="confirmed", tag_status="pending" if batch_tag else "not_requested",
                    batch_tag=batch_tag,
                )
            except Exception:
                with _state_lock(self.review_path):
                    self._append_locked({
                        "event": "create_uncertain", "schema_version": 1,
                        "event_id": str(uuid.uuid4()), "attempt_id": attempt_id,
                        "candidate_id": candidate_id, "title": view.candidate.name,
                        "source_hash": view.source_hash, "source_path": view.source_path,
                        "batch_tag": batch_tag, "slug": slug,
                        "category": "local_state_write_failed", "created_at": _now(),
                    })
                outcomes.append(self._outcome(candidate_id, view.candidate.name, "uncertain",
                                              reason="Mealie responded, but local import history could not be saved."))
                continue
            try:
                with _state_lock(self.review_path):
                    self._append_locked({
                        "event": "creation_confirmed", "schema_version": 1,
                        "event_id": str(uuid.uuid4()), "attempt_id": attempt_id,
                        "candidate_id": candidate_id, "title": view.candidate.name,
                        "source_hash": view.source_hash, "source_path": view.source_path,
                        "extraction_id": view.extraction_id, "revision": view.revision,
                        "content_sha256": digest, "slug": slug, "batch_tag": batch_tag,
                        "approval_method": planned.get("approval_method", ""),
                        "approval_rationale": planned.get("approval_rationale", ""),
                        "no_instructions_reason": planned.get("no_instructions_reason", ""),
                        "created_at": _now(),
                    })
            except Exception:
                outcomes.append(self._outcome(candidate_id, view.candidate.name, "uncertain",
                                              reason="Creation is protected in import history; audit recording needs repair.",
                                              slug=slug))
                continue
            if not batch_tag:
                with _state_lock(self.review_path):
                    self._append_locked({
                        "event": "tag_not_requested", "schema_version": 1,
                        "event_id": str(uuid.uuid4()), "candidate_id": candidate_id,
                        "slug": slug, "created_at": _now(),
                    })
                outcomes.append(self._outcome(candidate_id, view.candidate.name, "created",
                                              tag_status="not_requested", slug=slug))
                continue
            tag_result = self._apply_tag(candidate_id, view.candidate.name, slug, batch_tag, attempt_id)
            outcomes.append(self._outcome(candidate_id, view.candidate.name, "created",
                                          tag_status=tag_result["status"], tag_reason=tag_result.get("reason", ""),
                                          slug=slug))
        return outcomes

    def dispatch_cli_candidate(self, candidate_id: str, recipe: RecipeCandidate,
                               source_path: str | Path, source_hash: str,
                               extraction_id: str, create_callback,
                               batch_tag: str = "", tag_client: Any = None) -> dict:
        """Claim a single CLI candidate through the same durable duplicate gate."""
        candidate = RecipeCandidate.model_validate(recipe)
        candidate_id = str(candidate_id)
        source_path = str(source_path)
        attempt_id = str(uuid.uuid4())
        approval_provenance = {"approval_method": "", "approval_rationale": "",
                               "no_instructions_reason": ""}
        review = ReviewStore(self.review_path, state_path=self.state_path)
        with _state_lock(self.review_path):
            review._reload()
            events = self._read_events()
            try:
                protection = self._protection_snapshot(review, events)
            except ImportProtectionError:
                return {"status": "uncertain", "slug": "", "tag_status": "not_attempted",
                        "reason": "Import history is unreadable; candidate remains protected."}
            registration = next((item for item in review.registrations.get(candidate_id, [])
                                 if item.get("source_hash") == source_hash
                                 and item.get("extraction_id") == extraction_id), None)
            modern = any(item.get("source_hash") == source_hash
                         and item.get("extraction_id") == extraction_id
                         for item in review.revisions.get(candidate_id, []))
            if modern:
                if registration is None:
                    return {"status": "not_sent", "slug": "", "tag_status": "not_attempted",
                            "reason": "Approved review registration is unavailable."}
                effective = review.project_loaded(candidate_id, source_hash, extraction_id, candidate)
                approval_provenance = {
                    "approval_method": effective.approval_method,
                    "approval_rationale": effective.approval_rationale,
                    "no_instructions_reason": effective.no_instructions_reason,
                    "content_sha256": effective.content_sha256,
                }
                exact_candidate = RecipeCandidate.model_validate(effective.recipe)
                if (not any(str(item).strip() for item in exact_candidate.instructions)
                        and not self.supports_empty_instructions):
                    return {"status": "not_sent", "slug": "", "tag_status": "not_attempted",
                            "reason": "Empty-instructions import is disabled until support is verified for the installed Mealie endpoint. Set MEALIE_SUPPORTS_EMPTY_INSTRUCTIONS=true only after confirming it preserves an empty list."}
                if (effective.approval_method != "manual_source_unavailable"
                        and not self._source_matches(source_path, source_hash)):
                    return {"status": "not_sent", "slug": "", "tag_status": "not_attempted",
                            "reason": "Source scan is unavailable or no longer matches."}
                if (effective.approval_method == "manual_source_unavailable"
                        and Path(source_path).is_file()
                        and not self._source_matches(source_path, source_hash)):
                    return {"status": "not_sent", "slug": "", "tag_status": "not_attempted",
                            "reason": "Available source bytes do not match the registered source hash."}
                if (not effective.import_ready
                        or exact_candidate.model_dump(mode="json") != candidate.model_dump(mode="json")):
                    return {"status": "not_sent", "slug": "", "tag_status": "not_attempted",
                            "reason": "CLI payload does not match the exact approved review revision."}
                if self._empty_instruction_rejection(events, candidate_id, source_hash,
                                                     extraction_id, effective.revision,
                                                     effective.content_sha256):
                    return {"status": "not_sent", "slug": "", "tag_status": "not_attempted",
                            "reason": "Mealie rejected an empty-instructions recipe for this exact revision. Edit the recipe or verify endpoint support before another create attempt."}
                try:
                    group = candidate_identity_group(review, candidate_id, source_hash, extraction_id)
                    if group and group in protection["protected_groups"]:
                        return {"status": "already_done", "slug": "", "tag_status": "not_attempted",
                                "reason": "A linked extraction already has a protected Mealie outcome."}
                    protected = review._protected_import_outcome(
                        registration, state_records=protection["state_records"],
                        protected_ids=protection["protected_ids"])
                except ReviewConflict:
                    return {"status": "uncertain", "slug": "", "tag_status": "not_attempted",
                            "reason": "Import history is unreadable; candidate remains protected."}
                if review.identity_link_status(candidate_id, source_hash, extraction_id) != "registered" or protected:
                    return {"status": "already_done", "slug": "", "tag_status": "not_attempted",
                            "reason": "Candidate identity or prior import outcome is protected."}
            elif registration:
                if not is_complete(candidate):
                    return {"status": "not_sent", "slug": "", "tag_status": "not_attempted",
                            "reason": "Recipe is incomplete."}
                try:
                    protected = review._protected_import_outcome(
                        registration, state_records=protection["state_records"],
                        protected_ids=protection["protected_ids"])
                except ReviewConflict:
                    return {"status": "uncertain", "slug": "", "tag_status": "not_attempted",
                            "reason": "Import history is unreadable; candidate remains protected."}
                if protected:
                    status = "already_done" if protected.get("status") == "IMPORTED" else "uncertain"
                    return {"status": status, "slug": str(protected.get("slug", "")),
                            "tag_status": str(protected.get("tag_status", "")),
                            "reason": "A prior import outcome protects this candidate."}
            elif not is_complete(candidate):
                return {"status": "not_sent", "slug": "", "tag_status": "not_attempted",
                        "reason": "Recipe is incomplete."}
            state = StateStore(self.state_path)
            existing = self._state_for(state, candidate_id)
            if existing and str(existing.get("status", "")).upper() in {
                "IMPORTED", "UNCERTAIN", "NEEDS_MANUAL_CHECK",
            }:
                status = "already_done" if existing.get("status") == "IMPORTED" else "uncertain"
                return {"status": status, "slug": str(existing.get("slug", "")),
                        "tag_status": str(existing.get("tag_status", "")),
                        "reason": "A prior import outcome protects this candidate."}
            candidate_events = self._candidate_events(events, candidate_id)
            if self._service_protected(candidate_events):
                status = self._service_protected_status(candidate_events)
                return {"status": status, "slug": "", "tag_status": "not_attempted",
                        "reason": "A prior create attempt requires reconciliation or history review."}
            attempt_recipe = (effective.recipe if modern else candidate.model_dump(mode="json"))
            attempt_digest = (effective.content_sha256 if modern else _recipe_digest(attempt_recipe))
            attempt_evidence = {
                "authorization_mode": "approved_revision" if modern else "legacy_auto_import",
                "attempt_evidence_version": 2,
                "content_sha256": attempt_digest,
                "recipe_snapshot": attempt_recipe,
                "outbound_payload": schema_recipe(RecipeCandidate.model_validate(attempt_recipe)),
            }
            if modern:
                attempt_evidence["revision"] = effective.revision
            self._append_locked({
                "event": "intent_started", "schema_version": 1,
                "event_id": str(uuid.uuid4()), "attempt_id": attempt_id,
                "plan_id": "cli", "candidate_id": candidate_id,
                "source_hash": source_hash, "extraction_id": extraction_id,
                "title": candidate.name, "source_path": source_path,
                "batch_tag": str(batch_tag or "").strip(), "created_at": _now(),
                **attempt_evidence,
                **approval_provenance,
            })
        try:
            slug = create_callback(candidate)
            if not isinstance(slug, str) or not slug.strip():
                raise RuntimeError("invalid_create_response")
            slug = slug.strip()
        except Exception as exc:
            status, category, http_status = self._create_failure(exc)
            with _state_lock(self.review_path):
                if status == "uncertain":
                    StateStore(self.state_path).add(
                        candidate_id, "UNCERTAIN", "", source_path, candidate.name,
                        identity=candidate_id, source_hash=source_hash,
                        extraction_id=extraction_id, batch_tag=str(batch_tag or "").strip(),
                        locked=True,
                    )
                else:
                    StateStore(self.state_path).add(
                        candidate_id, "IMPORT_FAILED", "", source_path, candidate.name,
                        identity=candidate_id, source_hash=source_hash,
                        extraction_id=extraction_id, batch_tag=str(batch_tag or "").strip(),
                        locked=True,
                    )
                self._append_locked({
                    "event": "create_uncertain" if status == "uncertain" else "create_rejected",
                    "schema_version": 1, "event_id": str(uuid.uuid4()),
                    "attempt_id": attempt_id, "candidate_id": candidate_id,
                    "title": candidate.name, "source_hash": source_hash,
                    "extraction_id": extraction_id,
                    **attempt_evidence,
                    "empty_instructions_rejected": (
                        not any(str(item).strip() for item in candidate.instructions)
                        and status == "rejected"),
                    "source_path": source_path, "batch_tag": str(batch_tag or "").strip(),
                    "category": category, "http_status": http_status, "created_at": _now(),
                })
            return {"status": status, "slug": "", "tag_status": "not_attempted",
                    "reason": self._failure_message(category, http_status)}
        tag = str(batch_tag or "").strip()
        try:
            with _state_lock(self.review_path):
                StateStore(self.state_path).add(
                    candidate_id, "IMPORTED", slug, source_path, candidate.name,
                    identity=candidate_id, source_hash=source_hash, extraction_id=extraction_id,
                    creation_status="confirmed", tag_status="pending" if tag else "not_requested",
                    batch_tag=tag, locked=True,
                )
                self._append_locked({
                    "event": "creation_confirmed", "schema_version": 1,
                    "event_id": str(uuid.uuid4()), "attempt_id": attempt_id,
                    "candidate_id": candidate_id, "title": candidate.name,
                    "source_hash": source_hash, "source_path": source_path,
                    "extraction_id": extraction_id, "slug": slug, "batch_tag": tag,
                    **attempt_evidence,
                    **approval_provenance,
                    "created_at": _now(),
                })
                if not tag:
                    self._append_locked({
                        "event": "tag_not_requested", "schema_version": 1,
                        "event_id": str(uuid.uuid4()), "candidate_id": candidate_id,
                        "slug": slug, "created_at": _now(),
                    })
        except Exception:
            with _state_lock(self.review_path):
                try:
                    StateStore(self.state_path).add(
                        candidate_id, "UNCERTAIN", slug, source_path, candidate.name,
                        identity=candidate_id, source_hash=source_hash,
                        extraction_id=extraction_id, batch_tag=tag, locked=True,
                    )
                    self._append_locked({
                        "event": "create_uncertain", "schema_version": 1,
                        "event_id": str(uuid.uuid4()), "attempt_id": attempt_id,
                        "candidate_id": candidate_id, "title": candidate.name,
                        "source_hash": source_hash, "source_path": source_path,
                        "slug": slug, "batch_tag": tag,
                        "category": "local_state_write_failed", "created_at": _now(),
                    })
                except Exception:
                    pass
            return {"status": "uncertain", "slug": slug, "tag_status": "unknown",
                    "reason": "Mealie responded, but local import history could not be saved."}
        if not tag:
            return {"status": "created", "slug": slug, "tag_status": "not_requested", "reason": ""}
        result = self._apply_tag(candidate_id, candidate.name, slug, tag, attempt_id, client=tag_client)
        return {"status": "created", "slug": slug, "tag_status": result["status"],
                "reason": result.get("reason", "")}

    def history(self) -> list[dict]:
        with _state_lock(self.review_path):
            events = self._read_events()
            state = StateStore(self.state_path)
            rows: dict[str, dict] = {}
            for record in state.records.values():
                identity = str(record.get("identity") or record.get("key") or "")
                if not identity:
                    continue
                digest = str(record.get("source_hash", ""))
                rows[identity] = {
                    "candidate_id": identity, "title": str(record.get("title", "")),
                    "source": str(record.get("file", "")), "source_hash": digest,
                    "slug": str(record.get("slug", "")),
                    "creation_status": str(record.get("creation_status") or self._state_creation(record)),
                    "tag_status": str(record.get("tag_status", "unknown")),
                    "batch_tag": str(record.get("batch_tag") or record.get("tag_name", "")),
                    "timestamp": str(record.get("timestamp", "")),
                    "cache_available": self._cache_available(digest),
                }
            for event in events:
                identity = str(event.get("candidate_id", ""))
                if not identity:
                    continue
                row = rows.setdefault(identity, {
                    "candidate_id": identity, "title": str(event.get("title", "")),
                    "source": str(event.get("source_path", "")),
                    "source_hash": str(event.get("source_hash", "")), "slug": "",
                    "creation_status": "not_started", "tag_status": "not_attempted",
                    "batch_tag": str(event.get("batch_tag", "")),
                    "timestamp": str(event.get("created_at", "")), "cache_available": False,
                    "approval_method": "", "approval_rationale": "",
                    "no_instructions_reason": "",
                })
                kind = event.get("event")
                if kind in {"intent_started", "create_uncertain"}:
                    for provenance_key in ("attempt_id", "revision", "content_sha256"):
                        if event.get(provenance_key) is not None:
                            row[provenance_key] = event[provenance_key]
                    if event.get("extraction_id"):
                        row["extraction_id"] = str(event["extraction_id"])
                    if event.get("event") == "intent_started":
                        row["attempt_status"] = "orphaned"
                        row["provider_response_evidence"] = ""
                        row["attempt_snapshot"] = event.get("recipe_snapshot")
                        row["historical_content_unavailable"] = (
                            event.get("plan_id") == "cli" and event.get("schema_version") == 1
                            and not event.get("authorization_mode")
                            and all(event.get(key) in (None, "") for key in (
                                "attempt_evidence_version", "content_sha256", "recipe_snapshot", "outbound_payload",
                            ))
                        )
                for provenance_key in ("approval_method", "approval_rationale", "no_instructions_reason"):
                    if event.get(provenance_key):
                        row[provenance_key] = str(event[provenance_key])
                if kind == "intent_started" and row.get("creation_status") not in {
                    "created", "confirmed", "linked_existing", "already_created",
                }:
                    row["creation_status"] = "uncertain"
                elif kind == "create_uncertain" and row.get("creation_status") not in {
                    "created", "confirmed", "linked_existing", "already_created",
                }:
                    row["creation_status"] = "uncertain"
                    row["attempt_status"] = "uncertain"
                    row["slug"] = str(event.get("slug", row.get("slug", "")))
                    row["provider_response_evidence"] = self._provider_evidence(event)
                elif kind == "create_rejected":
                    row["creation_status"] = "rejected"
                    row["attempt_status"] = "rejected"
                    row["provider_response_evidence"] = self._provider_evidence(event)
                elif kind in {"creation_confirmed", "reconciled_existing"}:
                    row["creation_status"] = "created" if kind == "creation_confirmed" else "linked_existing"
                    row["attempt_status"] = "confirmed" if kind == "creation_confirmed" else "reconciled"
                    row["slug"] = str(event.get("slug", row.get("slug", "")))
                    row["tag_status"] = "pending" if event.get("batch_tag") else "not_requested"
                    row["batch_tag"] = str(event.get("batch_tag", row.get("batch_tag", "")))
                elif kind in {"tag_started", "tag_unknown"}:
                    row["tag_status"] = "unknown"
                elif kind == "tagged":
                    row["tag_status"] = "tagged"
                elif kind == "tag_failed":
                    row["tag_status"] = "failed"
                elif kind == "reconciliation_action_started":
                    row["reconciliation_feedback"] = {
                        "action_id": str(event.get("action_id", "")),
                        "attempt_id": str(event.get("attempt_id", "")),
                        "slug": str(event.get("slug", "")),
                        "action": str(event.get("action", "")),
                        "result_status": "in_progress",
                        "reason": "The selected Mealie recipe is being checked.",
                        "protection_active": True,
                        "next_step": "Wait for the result; do not retry the protected recipe.",
                        "created_at": str(event.get("created_at", "")),
                    }
                elif kind == "reconciliation_action_result":
                    row["reconciliation_feedback"] = {
                        "action_id": str(event.get("action_id", "")),
                        "attempt_id": str(event.get("attempt_id", "")),
                        "slug": str(event.get("slug", "")),
                        "action": str(event.get("action", "")),
                        "result_status": str(event.get("result_status", "")),
                        "reason": str(event.get("reason", "")),
                        "protection_active": bool(event.get("protection_active", True)),
                        "next_step": str(event.get("next_step", "")),
                        "created_at": str(event.get("created_at", "")),
                    }
                elif kind == "reconciliation_action_dismissed":
                    feedback = row.get("reconciliation_feedback") or {}
                    if feedback.get("action_id") == str(event.get("action_id", "")):
                        row.pop("reconciliation_feedback", None)
                if event.get("created_at"):
                    row["timestamp"] = str(event["created_at"])
                if event.get("source_hash"):
                    row["cache_available"] = self._cache_available(str(event["source_hash"]))
            return sorted(rows.values(), key=lambda item: str(item.get("timestamp", "")), reverse=True)

    def repair_tag(self, candidate_id: str, *, expected_tag: str = "") -> dict:
        self._require_client()
        with _state_lock(self.review_path):
            events = self._read_events()
            state = StateStore(self.state_path)
            record = self._state_for(state, candidate_id)
            candidate_events = self._candidate_events(events, candidate_id)
            confirmations = [item for item in candidate_events
                             if item.get("event") in {"creation_confirmed", "reconciled_existing"}]
            identities = {(str(item.get("slug", "")), str(item.get("batch_tag", "")))
                          for item in confirmations}
            if len(identities) > 1:
                return self._outcome(candidate_id, "", "not_available",
                                     reason="Durable creation history contains conflicting recipe or tag identities.")
            created = confirmations[-1] if confirmations else None
            for confirmation in confirmations:
                if confirmation.get("event") == "reconciled_existing":
                    try:
                        verified, _ = self._uncertain_attempt(
                            events, candidate_id, str(confirmation.get("attempt_id", "")))
                    except Exception:
                        verified = None
                    if (verified is None or not confirmation.get("reviewer_confirmed")
                            or not str(confirmation.get("reviewer_evidence", "")).strip()
                            or (confirmation.get("historical_content_unavailable")
                                and not confirmation.get("historical_content_ack"))):
                        return self._outcome(candidate_id, "", "not_available",
                                             reason="The reconciliation record lacks verifiable reviewer and attempt evidence.")
                elif confirmation.get("event") == "creation_confirmed":
                    attempt = str(confirmation.get("attempt_id", ""))
                    intents = [item for item in candidate_events
                               if item.get("event") == "intent_started" and item.get("attempt_id") == attempt]
                    if (not attempt or len(intents) != 1
                            or any(intents[0].get(key) != confirmation.get(key)
                                   for key in ("candidate_id", "source_hash", "extraction_id", "title", "batch_tag"))):
                        return self._outcome(candidate_id, "", "not_available",
                                             reason="The creation record does not match one durable import intent.")
            slug = str((created or {}).get("slug") or (record or {}).get("slug", ""))
            tag = str((created or {}).get("batch_tag")
                      or (record or {}).get("batch_tag") or (record or {}).get("tag_name", ""))
            title = str((created or {}).get("title") or (record or {}).get("title", ""))
            if (not slug or not tag or (not created and
                    (not record or record.get("status") != "IMPORTED"))):
                return self._outcome(candidate_id, title, "not_available",
                                     reason="No confirmed imported recipe with a pending batch tag was found.")
            if record and (record.get("status") != "IMPORTED"
                           or str(record.get("slug", "")) != slug
                           or (record.get("batch_tag") or record.get("tag_name")) not in (None, "", tag)):
                return self._outcome(candidate_id, title, "not_available", slug=slug,
                                     reason="Compatibility history conflicts with the durable recipe or tag identity.")
            if expected_tag and tag != str(expected_tag).strip():
                return self._outcome(candidate_id, title, "not_available", slug=slug,
                                     reason="The imported recipe's recorded batch tag does not match the selected tag scope.")
            if self._latest_tag_status(candidate_events) == "tagged" or (record or {}).get("tag_status") == "tagged":
                return self._outcome(candidate_id, title, "already_tagged", tag_status="tagged", slug=slug)
            attempt_id = str(uuid.uuid4())
        result = self._apply_tag(candidate_id, title, slug, tag, attempt_id)
        return self._outcome(candidate_id, title, "tag_repair", tag_status=result["status"],
                             tag_reason=result.get("reason", ""), slug=slug)

    def begin_reconciliation_action(self, *, candidate_id: str, action_id: str,
                                    action: str, attempt_id: str = "", slug: str = "") -> dict:
        candidate_id, action_id = str(candidate_id).strip(), str(action_id).strip()
        if not candidate_id or not action_id:
            raise ValueError("A candidate ID and stable action ID are required for recovery feedback.")
        event = {
            "event": "reconciliation_action_started", "schema_version": 1,
            "event_id": str(uuid.uuid4()), "action_id": action_id,
            "candidate_id": candidate_id, "attempt_id": str(attempt_id or ""),
            "slug": str(slug or "")[:200], "action": str(action or "")[:80],
            "created_at": _now(),
        }
        with _state_lock(self.review_path):
            events = self._read_events()
            prior = next((item for item in events
                          if item.get("event") == "reconciliation_action_started"
                          and item.get("action_id") == action_id), None)
            if prior:
                if any(prior.get(key) != event.get(key) for key in (
                    "candidate_id", "attempt_id", "slug", "action",
                )):
                    raise ImportServiceError("The recovery action ID was reused for another request.")
                return prior
            self._append_locked(event)
        return event

    def record_reconciliation_feedback(self, *, candidate_id: str, action_id: str,
                                       action: str, result_status: str, reason: str,
                                       attempt_id: str = "", slug: str = "",
                                       next_step: str = "", protection_active: bool = True) -> dict:
        """Persist the latest local recovery action without changing import authority."""
        candidate_id, action_id = str(candidate_id).strip(), str(action_id).strip()
        if not candidate_id or not action_id:
            raise ValueError("A candidate ID and stable action ID are required for recovery feedback.")
        event = {
            "event": "reconciliation_action_result", "schema_version": 1,
            "event_id": str(uuid.uuid4()), "action_id": action_id,
            "candidate_id": candidate_id, "attempt_id": str(attempt_id or ""),
            "slug": str(slug or "")[:200], "action": str(action or "")[:80],
            "result_status": str(result_status or "failed")[:80],
            "reason": str(reason or "")[:500],
            "protection_active": bool(protection_active),
            "next_step": str(next_step or "")[:500], "created_at": _now(),
        }
        with _state_lock(self.review_path):
            events = self._read_events()
            started = next((item for item in events
                            if item.get("event") == "reconciliation_action_started"
                            and item.get("action_id") == action_id), None)
            if not started or started.get("candidate_id") != candidate_id:
                raise ImportServiceError("The recovery action was not durably started before its result.")
            prior = next((item for item in reversed(events)
                          if item.get("event") == "reconciliation_action_result"
                          and item.get("action_id") == action_id), None)
            if prior:
                if any(prior.get(key) != event.get(key) for key in (
                    "candidate_id", "attempt_id", "slug", "action", "result_status",
                    "reason", "protection_active", "next_step",
                )):
                    raise ImportServiceError("The recovery action ID was reused with different result data.")
                return prior
            self._append_locked(event)
        return event

    def dismiss_reconciliation_feedback(self, candidate_id: str, action_id: str) -> None:
        candidate_id, action_id = str(candidate_id), str(action_id)
        with _state_lock(self.review_path):
            events = self._read_events()
            latest = next((item for item in reversed(events)
                           if item.get("event") in {"reconciliation_action_result",
                                                     "reconciliation_action_started"}
                           and item.get("candidate_id") == candidate_id), None)
            if not latest or latest.get("action_id") != action_id:
                return
            if any(item.get("event") == "reconciliation_action_dismissed"
                   and item.get("action_id") == action_id for item in events):
                return
            self._append_locked({
                "event": "reconciliation_action_dismissed", "schema_version": 1,
                "event_id": str(uuid.uuid4()), "action_id": action_id,
                "candidate_id": candidate_id, "created_at": _now(),
            })

    def reconcile_existing(self, candidate_id: str, slug: str, attempt_id: str = "", *,
                           reviewer_confirmed: bool = False, evidence: str = "",
                           material_difference_ack: bool = False,
                           unavailable_field_ack: bool = False,
                           historical_content_ack: bool = False,
                           observed_recipe_digest: str = "") -> dict:
        """Inspect one uncertain attempt, then link only after explicit evidence and comparison."""
        self._require_client()
        candidate_id, slug, attempt_id = str(candidate_id), str(slug).strip(), str(attempt_id).strip()
        if not slug:
            return self._outcome(candidate_id, "", "not_verified", reason="A Mealie slug is required.")
        with _state_lock(self.review_path):
            events = self._read_events()
        projection, reason = self._uncertain_attempt(events, candidate_id, attempt_id)
        if projection is None:
            return self._outcome(candidate_id, "", "not_verified", reason=reason)
        with _state_lock(self.review_path):
            attempt_id = projection["attempt_id"]
            existing_link = next((item for item in projection["events"]
                                  if item.get("event") == "reconciled_existing"), None)
            if existing_link:
                return self._outcome(candidate_id, projection["title"], "already_linked",
                                     slug=str(existing_link.get("slug", "")), attempt_id=attempt_id)

        try:
            mealie_recipe = self.client.get_recipe(slug)
        except Exception:
            return self._outcome(candidate_id, projection["title"], "not_verified",
                                 reason="Mealie could not verify that selected recipe identity.",
                                 attempt_id=attempt_id)
        if not mealie_recipe:
            with _state_lock(self.review_path):
                self._append_locked({
                    "event": "reconcile_not_found", "schema_version": 1,
                    "event_id": str(uuid.uuid4()), "attempt_id": attempt_id,
                    "candidate_id": candidate_id, "slug": slug, "created_at": _now(),
                })
            return self._outcome(candidate_id, projection["title"], "not_proven_absent",
                                 reason="A lookup miss does not prove the recipe was not created.",
                                 attempt_id=attempt_id)
        if (not isinstance(mealie_recipe, dict) or not mealie_recipe.get("id")
                or mealie_recipe.get("slug") != slug):
            return self._outcome(candidate_id, projection["title"], "not_verified",
                                 reason="Mealie did not return the selected recipe ID and slug.",
                                 attempt_id=attempt_id)

        comparison = self._compare_reconciliation_recipe(projection["recipe_snapshot"], mealie_recipe)
        comparison_digest = self._reconciliation_digest(mealie_recipe)
        if not reviewer_confirmed:
            return self._outcome(candidate_id, projection["title"], "confirmation_required",
                                 reason="Compare both recipes and confirm the identity with reviewer evidence.",
                                 attempt_id=attempt_id, comparison=comparison,
                                 comparison_digest=comparison_digest, slug=slug,
                                 historical_content_unavailable=projection.get("historical_content_unavailable", False))
        if ((projection.get("historical_content_unavailable") and len(str(evidence).strip()) < 40)
                or (not projection.get("historical_content_unavailable") and not str(evidence).strip())):
            return self._outcome(candidate_id, projection["title"], "not_verified",
                                 reason=("Provide specific reviewer evidence beyond a title match or general familiarity."
                                         if projection.get("historical_content_unavailable") else
                                         "Reviewer evidence is required to link this existing recipe."),
                                 attempt_id=attempt_id, comparison=comparison,
                                 comparison_digest=comparison_digest, slug=slug)
        if observed_recipe_digest != comparison_digest:
            return self._outcome(candidate_id, projection["title"], "not_verified",
                                 reason="The Mealie recipe changed after it was displayed. Inspect it again before confirming.",
                                 attempt_id=attempt_id, comparison=comparison,
                                 comparison_digest=comparison_digest, slug=slug)
        if comparison["material_differences"] and not material_difference_ack:
            return self._outcome(candidate_id, projection["title"], "not_verified",
                                 reason="Recipe content differences require explicit acknowledgment.",
                                 attempt_id=attempt_id, comparison=comparison,
                                 comparison_digest=comparison_digest, slug=slug)
        if comparison["unavailable_fields"] and not unavailable_field_ack:
            return self._outcome(candidate_id, projection["title"], "not_verified",
                                 reason="Mealie did not return every compared field; explicitly acknowledge the unavailable evidence.",
                                 attempt_id=attempt_id, comparison=comparison,
                                 comparison_digest=comparison_digest, slug=slug)
        if projection.get("historical_content_unavailable") and not historical_content_ack:
            return self._outcome(candidate_id, projection["title"], "not_verified",
                                 reason="Acknowledge that immutable content evidence from this historical attempt is unavailable.",
                                 attempt_id=attempt_id, comparison=comparison,
                                 comparison_digest=comparison_digest, slug=slug,
                                 historical_content_unavailable=True)

        with _state_lock(self.review_path):
            latest_events = self._read_events()
            latest, reason = self._uncertain_attempt(latest_events, candidate_id, attempt_id)
            if latest is None:
                return self._outcome(candidate_id, projection["title"], "not_verified",
                                     reason=reason, attempt_id=attempt_id)
            existing_link = next((item for item in latest["events"]
                                  if item.get("event") == "reconciled_existing"), None)
            if existing_link:
                return self._outcome(candidate_id, projection["title"], "already_linked",
                                     slug=str(existing_link.get("slug", "")), attempt_id=attempt_id)
            self._append_locked({
                "event": "reconciled_existing", "schema_version": 1,
                "event_id": str(uuid.uuid4()), "attempt_id": attempt_id,
                "candidate_id": candidate_id, "slug": slug,
                "mealie_recipe_id": str(mealie_recipe["id"]),
                "title": projection["title"], "source_hash": projection["source_hash"],
                "source_path": projection["source_path"],
                "extraction_id": projection["extraction_id"],
                "revision": projection["revision"],
                "content_sha256": projection["content_sha256"],
                "batch_tag": projection["batch_tag"],
                "mealie_recipe_digest": comparison_digest,
                "reviewer_confirmed": True,
                "reviewer_evidence": str(evidence).strip()[:1000],
                "material_difference_ack": bool(material_difference_ack),
                "unavailable_field_ack": bool(unavailable_field_ack),
                "historical_content_unavailable": bool(projection.get("historical_content_unavailable")),
                "historical_content_ack": bool(historical_content_ack),
                "created_at": _now(),
            })
        try:
            StateStore(self.state_path).add(
                candidate_id, "IMPORTED", slug, projection["source_path"], projection["title"],
                identity=candidate_id, source_hash=projection["source_hash"],
                extraction_id=projection["extraction_id"],
                review_revision=projection["revision"],
                content_sha256=projection["content_sha256"],
                creation_status="linked_existing", tag_status="pending" if projection["batch_tag"] else "not_requested",
                batch_tag=projection["batch_tag"],
            )
        except Exception:
            return self._outcome(candidate_id, projection["title"], "linked_existing",
                                 reason="Existing recipe was verified and durably protected; compatibility history needs repair.",
                                 slug=slug, attempt_id=attempt_id)
        return self._outcome(candidate_id, projection["title"], "linked_existing",
                             slug=slug, attempt_id=attempt_id, comparison=comparison)

    def _apply_tag(self, candidate_id: str, title: str, slug: str,
                   tag_name: str, attempt_id: str, client: Any = None) -> dict:
        tag_client = client or self.client
        if tag_client is None:
            return {"status": "failed", "reason": "A Mealie tag client was not supplied."}
        with _state_lock(self.review_path):
            self._append_locked({
                "event": "tag_started", "schema_version": 1, "event_id": str(uuid.uuid4()),
                "attempt_id": attempt_id, "candidate_id": candidate_id, "slug": slug,
                "batch_tag": tag_name, "title": title, "created_at": _now(),
            })
        category = ""
        http_status = None
        updating_recipe = False
        try:
            tag = tag_client.ensure_tag(tag_name)
            updating_recipe = True
            result = tag_client.add_tag(slug, tag)
            status = "already_tagged" if result == "already_present" else "tagged"
            event = {
                "event": "tagged", "schema_version": 1, "event_id": str(uuid.uuid4()),
                "attempt_id": attempt_id, "candidate_id": candidate_id, "title": title,
                "slug": slug, "batch_tag": tag_name, "created_at": _now(),
            }
        except Exception as exc:
            _, category, http_status = self._create_failure(exc)
            status = ("unknown" if updating_recipe and category in {
                "timeout", "server_error", "request_outcome_unknown",
            } else "failed")
            event = {
                "event": "tag_unknown" if status == "unknown" else "tag_failed",
                "schema_version": 1, "event_id": str(uuid.uuid4()),
                "attempt_id": attempt_id, "candidate_id": candidate_id, "title": title,
                "slug": slug, "batch_tag": tag_name, "category": category,
                "http_status": http_status, "created_at": _now(),
            }
        with _state_lock(self.review_path):
            self._append_locked(event)
        try:
            state = StateStore(self.state_path)
            old = self._state_for(state, candidate_id) or {}
            if not old:
                old = next((item for item in reversed(self._candidate_events(
                    self._read_events(), candidate_id))
                    if item.get("event") in {"creation_confirmed", "reconciled_existing"}
                    and item.get("slug") == slug), {})
            state.add(
                candidate_id, "IMPORTED", slug, str(old.get("file", old.get("source_path", ""))), title,
                identity=candidate_id, source_hash=str(old.get("source_hash", "")),
                extraction_id=str(old.get("extraction_id", "")),
                review_revision=old.get("review_revision", old.get("revision")),
                content_sha256=str(old.get("content_sha256", "")),
                import_operation_id=str(old.get("import_operation_id", "")),
                creation_status=str(old.get("creation_status", "confirmed")),
                tag_status=("tagged" if status in {"tagged", "already_tagged"}
                            else "unknown" if status == "unknown" else "failed"),
                batch_tag=tag_name,
            )
        except Exception:
            if status in {"tagged", "already_tagged"}:
                return {"status": "unknown", "reason": "Tag update succeeded but local history could not be updated."}
        return {
            "status": status if status in {"tagged", "already_tagged", "unknown"} else "failed",
            "reason": ("" if status in {"tagged", "already_tagged"} else
                       "Tag update outcome is unknown; inspect the recipe in Mealie before retrying."
                       if status == "unknown" else self._failure_message(category, http_status)),
        }

    def _eligibility(self, review: ReviewStore, events: list[dict],
                     view: RecipeView | None,
                     protection: dict | None = None) -> tuple[str, dict | None]:
        if view is None:
            return "Candidate is no longer available.", None
        if view.revision < 1:
            return "UI import requires an explicitly approved review revision.", None
        effective = self._effective(review, view)
        if effective and self._empty_instruction_rejection(
                events, view.candidate_id, view.source_hash, view.extraction_id,
                effective.revision, effective.content_sha256):
            return ("Mealie rejected an empty-instructions recipe for this exact revision. Edit the recipe or verify endpoint support before another create attempt.", None)
        if (view.source_path and Path(view.source_path).is_file()
                and not self._source_matches(view.source_path, view.source_hash)):
            return "Source scan is unavailable or no longer matches.", None
        if effective is None or not effective.import_ready:
            return "Exact revision is no longer approved or has unresolved review items.", None
        if (not any(str(item).strip() for item in effective.recipe.get("instructions", []))
                and not self.supports_empty_instructions):
            return ("Empty-instructions import is disabled until support is verified for the installed Mealie endpoint. Set MEALIE_SUPPORTS_EMPTY_INSTRUCTIONS=true only after confirming it preserves an empty list.", None)
        if view.identity_status != "registered":
            return "Candidate identity needs reconciliation.", None
        if effective.approval_method != "manual_source_unavailable":
            if not view.source_path or not self._source_matches(view.source_path, view.source_hash):
                return "Source scan is unavailable or no longer matches.", None
        elif (view.source_path and Path(view.source_path).is_file()
              and not self._source_matches(view.source_path, view.source_hash)):
            return "Available source bytes do not match the registered source hash.", None
        if effective.revision != view.revision:
            return "Review changed while loading; refresh the import preflight.", None
        registration = self._registration(review, view)
        if not registration:
            return "Candidate registration is unavailable.", None
        if review.identity_link_status(view.candidate_id, view.source_hash, view.extraction_id) != "registered":
            return "Candidate identity needs reconciliation.", None
        if protection is None:
            try:
                protection = self._protection_snapshot(review, events)
            except ImportProtectionError:
                return "Import history is unreadable; candidate remains protected.", None
        group = candidate_identity_group(review, view.candidate_id, view.source_hash,
                                         view.extraction_id)
        if group and group in protection["protected_groups"]:
            return "A linked extraction already has a protected Mealie outcome.", None
        try:
            protected = review._protected_import_outcome(
                registration, state_records=protection["state_records"],
                protected_ids=protection["protected_ids"])
        except ReviewConflict:
            return "Import history is unreadable; candidate remains protected.", None
        if protected:
            return "Candidate already has a protected import outcome.", None
        if self._service_protected(self._candidate_events(events, view.candidate_id)):
            status = self._service_protected_status(self._candidate_events(events, view.candidate_id))
            return ("A prior create attempt is uncertain; check Mealie before retrying."
                    if status == "uncertain" else "Candidate was already created; use tag repair if needed."), None
        return "", effective.recipe

    def _protection_snapshot(self, review: ReviewStore, events: list[dict]) -> dict:
        """Read protection ledgers once while the review lock is held."""
        state_records = read_import_state(self.state_path)
        protected_ids = protected_candidate_ids_from_records(state_records, events)
        return {
            "state_records": state_records,
            "protected_ids": protected_ids,
            "protected_groups": protected_identity_groups(review, protected_ids),
        }

    @staticmethod
    def _empty_instruction_rejection(events: list[dict], candidate_id: str,
                                     source_hash: str, extraction_id: str,
                                     revision: int, content_sha256: str) -> bool:
        return any(item.get("event") == "create_rejected"
                   and item.get("empty_instructions_rejected") is True
                   and item.get("candidate_id") == candidate_id
                   and item.get("source_hash") == source_hash
                   and item.get("extraction_id") == extraction_id
                   and item.get("revision") == revision
                   and item.get("content_sha256") == content_sha256
                   for item in events)

    @staticmethod
    def _effective(review: ReviewStore, view: RecipeView):
        if ImportService._registration(review, view) is None:
            return None
        return review.project_loaded(view.candidate_id, view.source_hash, view.extraction_id,
                                     view.candidate, view.draft)

    @staticmethod
    def _registration(review: ReviewStore, view: RecipeView) -> dict | None:
        return next((item for item in review.registrations.get(view.candidate_id, [])
                     if item.get("source_hash") == view.source_hash
                     and item.get("extraction_id") == view.extraction_id), None)

    def _digest(self, review: ReviewStore, view: RecipeView) -> str:
        effective = self._effective(review, view)
        return str(effective.content_sha256) if effective else ""

    def _read_events(self) -> list[dict]:
        if not self.event_path.exists():
            return []
        try:
            events = [json.loads(line) for line in self.event_path.read_text(encoding="utf-8").splitlines()
                      if line.strip()]
            if any(not isinstance(item, dict) or not item.get("event") for item in events):
                raise ValueError("invalid event")
            return events
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise ImportServiceError(
                "Local import history is malformed; imports are blocked pending repair."
            ) from exc

    def _append_locked(self, event: dict) -> None:
        self._read_events()
        self.output_dir.mkdir(parents=True, exist_ok=True)
        with self.event_path.open("a", encoding="utf-8") as handle:
            handle.write(_encoded(event) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        saved = next((item for item in self._read_events()
                      if item.get("event_id") == event.get("event_id")), None)
        if saved is None or _encoded(saved) != _encoded(event):
            raise ImportServiceError("Import history readback failed; candidate remains protected.")

    def _plan(self, plan_id: str) -> dict | None:
        with _state_lock(self.review_path):
            return next((item for item in self._read_events()
                         if item.get("event") == "plan_created" and item.get("plan_id") == plan_id), None)

    @staticmethod
    def _candidate_events(events: list[dict], candidate_id: str) -> list[dict]:
        return [item for item in events if item.get("candidate_id") == candidate_id]

    def _uncertain_attempt(self, events: list[dict], candidate_id: str,
                           attempt_id: str = "") -> tuple[dict | None, str]:
        candidate_events = self._candidate_events(events, candidate_id)
        attempt_ids = list(dict.fromkeys(
            str(item.get("attempt_id", "")) for item in candidate_events
            if item.get("event") == "intent_started" and item.get("attempt_id")
        ))
        unresolved = []
        for value in attempt_ids:
            attempt_events = [item for item in candidate_events
                              if str(item.get("attempt_id", "")) == value]
            terminal = [item for item in attempt_events if item.get("event") in {
                "create_uncertain", "create_rejected", "creation_confirmed",
            }]
            links = [item for item in attempt_events if item.get("event") == "reconciled_existing"]
            if not terminal and not links:
                unresolved.append(value)
            elif (len(terminal) == 1 and terminal[0].get("event") == "create_uncertain"
                  and not links):
                unresolved.append(value)
        if attempt_id:
            if attempt_id not in attempt_ids:
                return None, "The selected uncertain attempt is missing from durable import history."
            selected_links = [item for item in candidate_events
                              if item.get("event") == "reconciled_existing"
                              and str(item.get("attempt_id", "")) == attempt_id]
            if len(unresolved) > 1 or (unresolved and attempt_id not in unresolved):
                return None, ("Import history contains multiple or conflicting unresolved intents. "
                              "Review the durable event history before continuing.")
            if len(selected_links) > 1:
                return None, "The selected attempt has duplicate reconciliation history and is protected."
            selected = attempt_id
        else:
            if len(unresolved) != 1:
                return None, ("Import history does not identify exactly one unresolved intent or uncertain attempt. "
                              "Review the durable event history before continuing.")
            selected = unresolved[0]

        attempt_events = [item for item in candidate_events
                          if str(item.get("attempt_id", "")) == selected]
        intents = [item for item in attempt_events if item.get("event") == "intent_started"]
        outcomes = [item for item in attempt_events if item.get("event") in {
            "create_uncertain", "create_rejected", "creation_confirmed",
        }]
        links = [item for item in attempt_events if item.get("event") == "reconciled_existing"]
        if len(intents) != 1 or len(outcomes) > 1:
            return None, "The selected import attempt has missing, duplicate, or contradictory create history and is protected."
        intent = intents[0]
        outcome = outcomes[0] if outcomes else None
        if outcome and outcome.get("event") not in {"create_uncertain", "reconciled_existing"}:
            return None, "The selected attempt is not an unresolved uncertain create."
        if len(links) == 1 and outcome is not None and outcome.get("event") != "create_uncertain":
            return None, "The selected attempt has contradictory creation and reconciliation history."
        if outcome is None and links:
            outcome_status = "reconciled"
        else:
            outcome_status = ("orphaned" if outcome is None else
                              "uncertain" if outcome.get("event") == "create_uncertain" else
                              "reconciled" if links else "uncertain")
        legacy_mode = intent.get("authorization_mode") == "legacy_auto_import"
        historical_mode = (
            intent.get("event") == "intent_started" and intent.get("plan_id") == "cli"
            and intent.get("schema_version") == 1
            and not intent.get("authorization_mode")
            and all(intent.get(key) in (None, "") for key in (
                "attempt_evidence_version", "content_sha256", "recipe_snapshot", "outbound_payload",
            ))
        )
        if historical_mode:
            required = ("attempt_id", "candidate_id", "source_hash", "extraction_id", "revision", "title")
            if any(intent.get(key) in (None, "") for key in required):
                return None, "The historical CLI attempt lacks durable candidate/source identity and remains protected."
            if (not re.fullmatch(r"[0-9a-f]{64}", str(intent.get("source_hash", "")))
                    or not re.fullmatch(r"[0-9a-f]{64}", str(intent.get("extraction_id", "")))):
                return None, "The historical attempt has invalid source/extraction identity and remains protected."
            try:
                review_events = [json.loads(line) for line in self.review_path.read_text(encoding="utf-8").splitlines()
                                 if line.strip()] if self.review_path.exists() else []
                registrations = [item for item in review_events
                                 if item.get("event") == "candidate_registration"
                                 and item.get("candidate_id") == candidate_id
                                 and item.get("source_hash") == intent.get("source_hash")
                                 and item.get("extraction_id") == intent.get("extraction_id")]
                revision = int(intent["revision"])
                candidate_snapshot = registrations[0].get("candidate") if len(registrations) == 1 else None
            except Exception:
                return None, "Historical candidate/source identity could not be verified; the attempt remains protected."
            if (len(registrations) != 1 or registrations[0].get("identity_status") != "registered"
                    or not isinstance(candidate_snapshot, dict)):
                return None, "The historical attempt does not have one verified candidate/source identity and remains protected."
            try:
                state_record = self._state_for(StateStore(self.state_path), candidate_id)
            except Exception:
                return None, "Compatibility import history is malformed; the historical attempt remains protected."
            if state_record and ((str(state_record.get("status", "")).upper() == "IMPORTED" and not links)
                                 or (links and state_record.get("slug") not in (None, "", links[0].get("slug")))):
                return None, "Compatibility history conflicts with the unresolved historical attempt; review local history."
            snapshot = candidate_snapshot
            if snapshot.get("name") != str(intent.get("title", "")):
                return None, "The historical title conflicts with the registered source candidate and remains protected."
            return {
                "attempt_id": selected, "events": attempt_events,
                "source_hash": str(intent["source_hash"]),
                "extraction_id": str(intent["extraction_id"]), "revision": revision,
                "content_sha256": "", "title": str(intent["title"]),
                "source_path": str(intent.get("source_path", "")),
                "batch_tag": str(intent.get("batch_tag", "")),
                "created_at": str(intent.get("created_at", "")),
                "outcome_status": outcome_status,
                "provider_response_evidence": self._provider_evidence(outcome) if outcome else "",
                "recipe_snapshot": snapshot,
                "authorization_mode": "historical_cli_incomplete_evidence",
                "historical_content_unavailable": True,
            }, ""
        required = ("attempt_id", "candidate_id", "source_hash", "extraction_id",
                    "content_sha256", "title") + (() if legacy_mode else ("revision",))
        if any(intent.get(key) in (None, "") for key in required):
            return None, "The selected attempt is missing required identity or revision fields and is protected."
        for key in ("candidate_id", "source_hash", "extraction_id", "revision",
                    "content_sha256", "title", "batch_tag"):
            if outcome and key in outcome and key in intent and outcome[key] != intent[key]:
                return None, f"The selected attempt has conflicting {key} history and is protected."
        if len(links) > 1:
            return None, "The selected attempt has duplicate reconciliation history and is protected."

        if legacy_mode:
            try:
                review_events = [json.loads(line) for line in self.review_path.read_text(encoding="utf-8").splitlines()
                                 if line.strip()] if self.review_path.exists() else []
                if any(not isinstance(item, dict) for item in review_events):
                    raise ValueError("invalid review history")
                registrations = [item for item in review_events
                                 if item.get("event") == "candidate_registration"
                                 and item.get("candidate_id") == candidate_id
                                 and item.get("source_hash") == intent.get("source_hash")
                                 and item.get("extraction_id") == intent.get("extraction_id")]
            except Exception as exc:
                return None, "Candidate registration history is unavailable; the legacy attempt remains protected."
            if len(registrations) != 1 or registrations[0].get("identity_status") != "registered":
                return None, "The legacy attempt does not have one verified source/extraction identity and remains protected."
            try:
                legacy_state_record = self._state_for(StateStore(self.state_path), candidate_id)
            except Exception:
                return None, "Compatibility import history is malformed; the legacy attempt remains protected."
            if legacy_state_record:
                if (str(legacy_state_record.get("status", "")).upper() == "IMPORTED" and not links):
                    return None, "Compatibility history conflicts with the uncertain legacy attempt; review local history."
                for intent_key, record_key in (("source_hash", "source_hash"),
                                               ("extraction_id", "extraction_id"),
                                               ("title", "title"), ("batch_tag", "batch_tag")):
                    if (intent.get(intent_key) not in (None, "")
                            and legacy_state_record.get(record_key) not in (None, "")
                            and str(intent[intent_key]) != str(legacy_state_record[record_key])):
                        return None, f"Compatibility history conflicts with the legacy attempt's {intent_key}; review local history."
            snapshot, snapshot_error = self._validated_attempt_snapshot(
                intent, {"recipe": intent.get("recipe_snapshot"),
                         "content_sha256": intent.get("content_sha256")},
            )
            if snapshot is None:
                return None, snapshot_error
            if snapshot.get("name") != intent.get("title"):
                return None, "The legacy attempt title conflicts with its durable recipe content and remains protected."
            return {
                "attempt_id": selected, "events": attempt_events,
                "source_hash": str(intent.get("source_hash", "")),
                "extraction_id": str(intent.get("extraction_id", "")),
                "revision": 0,
                "content_sha256": str(intent.get("content_sha256", "")),
                "title": str(intent.get("title", "")),
                "source_path": str(intent.get("source_path", "")),
                "batch_tag": str(intent.get("batch_tag", "")),
                "created_at": str(intent.get("created_at", "")),
                "outcome_status": outcome_status,
                "provider_response_evidence": self._provider_evidence(outcome) if outcome else "",
                "recipe_snapshot": snapshot,
                "authorization_mode": "legacy_auto_import",
            }, ""

        try:
            state = StateStore(self.state_path)
            record = self._state_for(state, candidate_id)
        except Exception:
            return None, "Compatibility import history is malformed; the candidate remains protected."
        if record:
            if str(record.get("status", "")).upper() == "IMPORTED" and not links:
                return None, "Compatibility history conflicts with the uncertain attempt; review local history."
            for key, record_key in (("source_hash", "source_hash"), ("extraction_id", "extraction_id"),
                                    ("revision", "review_revision"), ("content_sha256", "content_sha256"),
                                    ("title", "title"), ("batch_tag", "batch_tag")):
                if intent.get(key) not in (None, "") and record.get(record_key) not in (None, ""):
                    if str(intent[key]) != str(record[record_key]):
                        return None, f"Compatibility history conflicts with the attempt's {key}; review local history."

        try:
            revision_number = int(intent.get("revision", -1))
        except (TypeError, ValueError):
            return None, "The selected attempt has an invalid review revision and is protected."
        try:
            review_events = [json.loads(line) for line in self.review_path.read_text(encoding="utf-8").splitlines()
                             if line.strip()] if self.review_path.exists() else []
            if any(not isinstance(item, dict) for item in review_events):
                raise ValueError("invalid review history")
            match = [item for item in review_events
                     if item.get("event") == "review_revision"
                     and item.get("candidate_id") == candidate_id
                     and item.get("source_hash") == intent.get("source_hash")
                     and item.get("extraction_id") == intent.get("extraction_id")
                     and int(item.get("revision", -1)) == revision_number
                     and item.get("content_sha256") == intent.get("content_sha256")]
        except Exception:
            return None, "Review revision history is malformed; the uncertain attempt remains protected."
        if len(match) != 1 or not isinstance(match[0].get("recipe"), dict):
            return None, "The exact approved recipe snapshot is unavailable for this attempt; identity cannot be verified."
        snapshot, snapshot_error = self._validated_attempt_snapshot(intent, match[0])
        if snapshot is None:
            return None, snapshot_error
        return {
            "attempt_id": selected, "events": attempt_events,
            "source_hash": str(intent.get("source_hash", "")),
            "extraction_id": str(intent.get("extraction_id", "")),
            "revision": revision_number,
            "content_sha256": str(intent.get("content_sha256", "")),
            "title": str(intent.get("title", "")),
            "source_path": str(intent.get("source_path", "")),
            "batch_tag": str(intent.get("batch_tag", "")),
            "created_at": str(intent.get("created_at", "")),
            "outcome_status": outcome_status,
            "provider_response_evidence": self._provider_evidence(outcome) if outcome else "",
            "recipe_snapshot": snapshot,
        }, ""

    @staticmethod
    def _validated_attempt_snapshot(intent: dict, revision: dict) -> tuple[dict | None, str]:
        """Validate durable attempt evidence against the exact saved revision's content."""
        digest = str(intent.get("content_sha256", ""))
        saved = revision.get("recipe")
        snapshot = intent.get("recipe_snapshot")
        if not isinstance(saved, dict) or not isinstance(snapshot, dict):
            return None, "The exact approved recipe snapshot is unavailable for this attempt; identity cannot be verified."
        if (str(revision.get("content_sha256", "")) != digest
                or _recipe_digest(saved) != digest
                or _recipe_digest(snapshot) != digest):
            return None, "The attempted recipe snapshot does not match its recorded content digest and is protected."
        evidence_version = intent.get("attempt_evidence_version")
        outbound = intent.get("outbound_payload")
        if evidence_version is not None:
            if evidence_version != 2 or not isinstance(outbound, dict):
                return None, "The import attempt contains unsupported or incomplete versioned evidence and is protected."
            try:
                expected_payload = schema_recipe(RecipeCandidate.model_validate(snapshot))
            except Exception:
                return None, "The import attempt recipe snapshot is invalid and is protected."
            if outbound != expected_payload:
                return None, "The attempted Mealie payload conflicts with its recipe snapshot and is protected."
        if snapshot.get("name") != intent.get("title"):
            return None, "The attempted recipe title conflicts with its durable content and is protected."
        return snapshot, ""

    @staticmethod
    def _provider_evidence(event: dict | None) -> str:
        if not event:
            return ""
        values = []
        if event.get("category"):
            values.append(str(event["category"]))
        if event.get("http_status") is not None:
            values.append(f"HTTP {event['http_status']}")
        if event.get("slug"):
            values.append(f"slug {event['slug']}")
        return "; ".join(values)

    @classmethod
    def _compare_reconciliation_recipe(cls, local_recipe: dict, mealie_recipe: dict) -> dict:
        local_schema = schema_recipe(RecipeCandidate.model_validate(local_recipe))
        local = {
            "title": str(local_schema.get("name", "")),
            "ingredients": cls._string_items(local_schema.get("recipeIngredient", [])),
            "instructions": cls._string_items(local_schema.get("recipeInstructions", [])),
            "yield_text": cls._plain_text(local_schema.get("recipeYield", "")),
            "description": cls._plain_text(local_schema.get("description", "")),
            "notes": cls._string_items(local_recipe.get("notes", [])),
        }
        provider_values = {
            "title": mealie_recipe.get("name"),
            "ingredients": mealie_recipe.get("recipeIngredient"),
            "instructions": mealie_recipe.get("recipeInstructions"),
            "yield_text": mealie_recipe.get("recipeYield"),
            "description": mealie_recipe.get("description"),
            "notes": mealie_recipe.get("notes"),
        }
        provider_keys = {
            "title": "name", "ingredients": "recipeIngredient",
            "instructions": "recipeInstructions", "yield_text": "recipeYield",
            "description": "description", "notes": "notes",
        }
        unavailable = [field for field, key in provider_keys.items()
                       if key not in mealie_recipe or mealie_recipe.get(key) is None]
        returned = {
            "title": cls._plain_text(provider_values["title"]),
            "ingredients": cls._string_items(provider_values["ingredients"]),
            "instructions": cls._string_items(provider_values["instructions"]),
            "yield_text": cls._plain_text(provider_values["yield_text"]),
            "description": cls._plain_text(provider_values["description"]),
            "notes": cls._string_items(provider_values["notes"]),
        }
        differences = []
        for field in provider_keys:
            if field in unavailable:
                continue
            left = local[field]
            right = returned[field]
            if isinstance(left, list):
                matches = ([cls._normalize(item) for item in left]
                           == [cls._normalize(item) for item in right])
            else:
                matches = cls._normalize(left) == cls._normalize(right)
            if not matches:
                differences.append(field)
        material = list(differences)
        return {"local": local, "mealie": returned, "differences": differences,
                "material_differences": material, "unavailable_fields": unavailable,
                "complete_evidence": not unavailable,
                "is_match": not differences and not unavailable}

    @staticmethod
    def _plain_text(value: Any) -> str:
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, (int, float)):
            return str(value)
        if isinstance(value, dict):
            for key in ("text", "value", "name"):
                if value.get(key) is not None:
                    return ImportService._plain_text(value[key])
            return ""
        if isinstance(value, list):
            return " ".join(ImportService._plain_text(item) for item in value if item is not None).strip()
        return ""

    @staticmethod
    def _string_items(value: Any) -> list[str]:
        if isinstance(value, str):
            return [value.strip()] if value.strip() else []
        if isinstance(value, dict):
            if value.get("text"):
                return [str(value["text"]).strip()]
            for key in ("itemListElement", "recipeInstructions", "items"):
                if key in value:
                    return ImportService._string_items(value[key])
            return []
        if isinstance(value, list):
            result = []
            for item in value:
                result.extend(ImportService._string_items(item))
            return result
        return []

    @classmethod
    def _reconciliation_digest(cls, mealie_recipe: dict) -> str:
        normalized = {
            "evidence_version": 2,
            "id": str(mealie_recipe.get("id", "")),
            "slug": str(mealie_recipe.get("slug", "")),
            "name": str(mealie_recipe.get("name", "")),
            "ingredients": cls._string_items(mealie_recipe.get("recipeIngredient", [])),
            "instructions": cls._string_items(mealie_recipe.get("recipeInstructions", [])),
            "recipeYield": cls._plain_text(mealie_recipe.get("recipeYield")),
            "description": cls._plain_text(mealie_recipe.get("description")),
            "notes": cls._string_items(mealie_recipe.get("notes", [])),
            "available_fields": [key for key in (
                "name", "recipeIngredient", "recipeInstructions", "recipeYield",
                "description", "notes",
            ) if key in mealie_recipe and mealie_recipe.get(key) is not None],
        }
        payload = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def _service_protected(events: list[dict]) -> bool:
        if any(item.get("event") in {"creation_confirmed", "reconciled_existing", "create_uncertain"}
               for item in events):
            return True
        attempts: dict[str, str] = {}
        for item in events:
            attempt = str(item.get("attempt_id", ""))
            if item.get("event") == "intent_started":
                attempts[attempt] = "started"
            elif item.get("event") in {"create_rejected", "create_uncertain", "creation_confirmed"}:
                attempts[attempt] = str(item.get("event"))
        return "started" in attempts.values()

    @staticmethod
    def _service_protected_status(events: list[dict]) -> str:
        if any(item.get("event") in {"creation_confirmed", "reconciled_existing"} for item in events):
            return "already_created"
        return "uncertain"

    def _blocked_status(self, events: list[dict], candidate_id: str) -> str:
        candidate_events = self._candidate_events(events, candidate_id)
        if self._service_protected(candidate_events):
            return self._service_protected_status(candidate_events)
        record = self._state_for(StateStore(self.state_path), candidate_id)
        if record:
            status = str(record.get("status", "")).upper()
            if status == "IMPORTED":
                return "already_created"
            if status in {"UNCERTAIN", "NEEDS_MANUAL_CHECK"}:
                return "uncertain"
        return ""

    @staticmethod
    def _latest_tag_status(events: list[dict]) -> str:
        for event in reversed(events):
            if event.get("event") == "tagged":
                return "tagged"
            if event.get("event") == "tag_failed":
                return "failed"
            if event.get("event") == "tag_unknown":
                return "unknown"
        return ""

    @staticmethod
    def _is_uncertain(events: list[dict], record: dict | None) -> bool:
        if record and str(record.get("status", "")).upper() == "UNCERTAIN":
            return True
        if any(item.get("event") == "create_uncertain" for item in events):
            return True
        started = {str(item.get("attempt_id", "")) for item in events if item.get("event") == "intent_started"}
        finished = {str(item.get("attempt_id", "")) for item in events
                    if item.get("event") in {"create_rejected", "create_uncertain", "creation_confirmed"}}
        return bool(started - finished)

    @staticmethod
    def _state_for(state: StateStore, candidate_id: str) -> dict | None:
        direct = state.records.get(candidate_id)
        if direct:
            return direct
        matches = [item for item in state.records.values() if item.get("identity") == candidate_id]
        return matches[0] if len(matches) == 1 else None

    @staticmethod
    def _state_creation(record: dict) -> str:
        recorded = str(record.get("creation_status", "")).casefold()
        if recorded in {"confirmed", "created"}:
            return "created"
        if recorded == "linked_existing":
            return "linked_existing"
        if recorded == "uncertain":
            return "uncertain"
        status = str(record.get("status", "")).upper()
        if status == "IMPORTED":
            return "created"
        if status in {"UNCERTAIN", "NEEDS_MANUAL_CHECK"}:
            return "uncertain"
        if status == "IMPORT_FAILED":
            return "rejected"
        return status.casefold() or "unknown"

    def _cache_available(self, source_hash: str) -> bool:
        cache = self.output_dir / "extracted"
        return bool(re.fullmatch(r"[0-9a-f]{64}", source_hash)
                    and cache.exists() and any(cache.glob(f"{source_hash}*.json")))

    @staticmethod
    def _source_matches(path: str, expected_hash: str) -> bool:
        try:
            source = Path(path)
            return bool(expected_hash and source.is_file() and file_digest(source) == expected_hash)
        except OSError:
            return False

    @staticmethod
    def _normalize(text: str) -> str:
        return re.sub(r"\s+", " ", text.strip().casefold())

    @staticmethod
    def _create_failure(exc: Exception) -> tuple[str, str, int | None]:
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)
        if status is None:
            match = re.search(r"\bHTTP\s+(\d{3})\b", str(exc), re.IGNORECASE)
            status = int(match.group(1)) if match else None
        if isinstance(status, int) and 400 <= status < 500 and status != 408:
            return "rejected", "server_rejected", status
        if isinstance(status, int) and status >= 500:
            return "uncertain", "server_error", status
        if isinstance(exc, TimeoutError) or exc.__class__.__name__.casefold().endswith("timeout"):
            return "uncertain", "timeout", None
        return "uncertain", "request_outcome_unknown", status if isinstance(status, int) else None

    @staticmethod
    def _failure_message(category: str, status: int | None) -> str:
        if category == "server_rejected":
            return f"Mealie rejected recipe creation (HTTP {status})." if status else "Mealie rejected recipe creation."
        if category == "server_error":
            return f"Mealie returned HTTP {status}; check Mealie before retrying." if status else "Check Mealie before retrying."
        if category == "timeout":
            return "The request timed out; check Mealie before retrying."
        return "The request outcome is unknown; check Mealie before retrying."

    @staticmethod
    def _outcome(candidate_id: str, title: str, creation_status: str, *,
                 reason: str = "", error_category: str = "", http_status: int | None = None,
                 **fields) -> dict:
        result = {"candidate_id": candidate_id, "title": title,
                  "creation_status": creation_status, "reason": reason}
        if error_category:
            result["error_category"] = error_category
        if http_status is not None:
            result["http_status"] = http_status
        result.update(fields)
        return result

    def _require_client(self) -> None:
        if self.client is None:
            raise ImportServiceError("A Mealie client must be explicitly supplied for this operation.")
