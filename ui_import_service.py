"""Selected-recipe Mealie import and history operations."""
from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from import_recipes import StateStore, file_digest
from recipe_quality import RecipeCandidate, is_complete
from review_store import ReviewConflict, ReviewStore, _state_lock
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
                 *, review_path: Path | None = None, state_path: Path | None = None):
        self.output_dir = Path(output_dir)
        self.source_root = Path(source_root) if source_root is not None else None
        self.client = client
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
            for candidate_id in selected:
                view = views.get(candidate_id)
                reason, recipe = self._eligibility(review, events, view)
                if reason:
                    excluded.append({"candidate_id": candidate_id, "reason": reason})
                    continue
                eligible.append({
                    "candidate_id": candidate_id, "title": view.candidate.name,
                    "source_path": view.source_path, "source_hash": view.source_hash,
                    "extraction_id": view.extraction_id, "revision": view.revision,
                    "content_sha256": self._digest(review, view), "recipe": recipe,
                })
            self._append_locked({
                "event": "plan_created", "schema_version": 1, "event_id": str(uuid.uuid4()),
                "plan_id": plan_id, "created_at": _now(), "batch_tag": tag,
                "eligible": [{key: row[key] for key in (
                    "candidate_id", "title", "source_path", "source_hash", "extraction_id",
                    "revision", "content_sha256",
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
                reason, recipe = self._eligibility(review, events, view)
                if reason:
                    blocked_status = self._blocked_status(events, candidate_id)
                    outcomes.append(self._outcome(candidate_id, planned.get("title", ""),
                                                  blocked_status or "not_sent", reason=reason))
                    continue
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
        if not is_complete(candidate):
            return {"status": "not_sent", "slug": "", "tag_status": "not_attempted",
                    "reason": "Recipe is incomplete."}
        attempt_id = str(uuid.uuid4())
        review = ReviewStore(self.review_path, state_path=self.state_path)
        with _state_lock(self.review_path):
            review._reload()
            events = self._read_events()
            registration = next((item for item in review.registrations.get(candidate_id, [])
                                 if item.get("source_hash") == source_hash
                                 and item.get("extraction_id") == extraction_id), None)
            modern = any(item.get("source_hash") == source_hash
                         and item.get("extraction_id") == extraction_id
                         for item in review.revisions.get(candidate_id, []))
            if modern:
                if not self._source_matches(source_path, source_hash):
                    return {"status": "not_sent", "slug": "", "tag_status": "not_attempted",
                            "reason": "Source scan is unavailable or no longer matches."}
                if registration is None:
                    return {"status": "not_sent", "slug": "", "tag_status": "not_attempted",
                            "reason": "Approved review registration is unavailable."}
                effective = review.project_loaded(candidate_id, source_hash, extraction_id, candidate)
                exact_candidate = RecipeCandidate.model_validate(effective.recipe)
                if (not effective.import_ready
                        or exact_candidate.model_dump(mode="json") != candidate.model_dump(mode="json")):
                    return {"status": "not_sent", "slug": "", "tag_status": "not_attempted",
                            "reason": "CLI payload does not match the exact approved review revision."}
                try:
                    protected = review._protected_import_outcome(registration)
                except ReviewConflict:
                    return {"status": "uncertain", "slug": "", "tag_status": "not_attempted",
                            "reason": "Import history is unreadable; candidate remains protected."}
                if registration.get("identity_status") != "registered" or protected:
                    return {"status": "already_done", "slug": "", "tag_status": "not_attempted",
                            "reason": "Candidate identity or prior import outcome is protected."}
            elif registration:
                try:
                    protected = review._protected_import_outcome(registration)
                except ReviewConflict:
                    return {"status": "uncertain", "slug": "", "tag_status": "not_attempted",
                            "reason": "Import history is unreadable; candidate remains protected."}
                if protected:
                    status = "already_done" if protected.get("status") == "IMPORTED" else "uncertain"
                    return {"status": status, "slug": str(protected.get("slug", "")),
                            "tag_status": str(protected.get("tag_status", "")),
                            "reason": "A prior import outcome protects this candidate."}
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
            self._append_locked({
                "event": "intent_started", "schema_version": 1,
                "event_id": str(uuid.uuid4()), "attempt_id": attempt_id,
                "plan_id": "cli", "candidate_id": candidate_id,
                "source_hash": source_hash, "extraction_id": extraction_id,
                "revision": max((int(item.get("revision", 0))
                                 for item in review.revisions.get(candidate_id, [])
                                 if item.get("source_hash") == source_hash
                                 and item.get("extraction_id") == extraction_id), default=0),
                "title": candidate.name, "source_path": source_path,
                "batch_tag": str(batch_tag or "").strip(), "created_at": _now(),
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
                })
                kind = event.get("event")
                if kind == "intent_started" and row.get("creation_status") not in {
                    "created", "confirmed", "linked_existing", "already_created",
                }:
                    row["creation_status"] = "uncertain"
                elif kind == "create_uncertain" and row.get("creation_status") not in {
                    "created", "confirmed", "linked_existing", "already_created",
                }:
                    row["creation_status"] = "uncertain"
                    row["slug"] = str(event.get("slug", row.get("slug", "")))
                elif kind == "create_rejected":
                    row["creation_status"] = "rejected"
                elif kind in {"creation_confirmed", "reconciled_existing"}:
                    row["creation_status"] = "created" if kind == "creation_confirmed" else "linked_existing"
                    row["slug"] = str(event.get("slug", row.get("slug", "")))
                    row["tag_status"] = "pending" if event.get("batch_tag") else "not_requested"
                    row["batch_tag"] = str(event.get("batch_tag", row.get("batch_tag", "")))
                elif kind == "tag_started":
                    row["tag_status"] = "unknown"
                elif kind == "tagged":
                    row["tag_status"] = "tagged"
                elif kind == "tag_failed":
                    row["tag_status"] = "failed"
                if event.get("created_at"):
                    row["timestamp"] = str(event["created_at"])
                if event.get("source_hash"):
                    row["cache_available"] = self._cache_available(str(event["source_hash"]))
            return sorted(rows.values(), key=lambda item: str(item.get("timestamp", "")), reverse=True)

    def repair_tag(self, candidate_id: str) -> dict:
        self._require_client()
        with _state_lock(self.review_path):
            events = self._read_events()
            state = StateStore(self.state_path)
            record = self._state_for(state, candidate_id)
            candidate_events = self._candidate_events(events, candidate_id)
            created = next((item for item in reversed(candidate_events)
                            if item.get("event") in {"creation_confirmed", "reconciled_existing"}), None)
            slug = str((created or {}).get("slug") or (record or {}).get("slug", ""))
            tag = str((created or {}).get("batch_tag")
                      or (record or {}).get("batch_tag") or (record or {}).get("tag_name", ""))
            title = str((created or {}).get("title") or (record or {}).get("title", ""))
            if not slug or not tag or not record or record.get("status") != "IMPORTED":
                return self._outcome(candidate_id, title, "not_available",
                                     reason="No confirmed imported recipe with a pending batch tag was found.")
            if self._latest_tag_status(candidate_events) == "tagged" or record.get("tag_status") == "tagged":
                return self._outcome(candidate_id, title, "already_tagged", tag_status="tagged", slug=slug)
            attempt_id = str(uuid.uuid4())
        result = self._apply_tag(candidate_id, title, slug, tag, attempt_id)
        return self._outcome(candidate_id, title, "tag_repair", tag_status=result["status"],
                             tag_reason=result.get("reason", ""), slug=slug)

    def reconcile_existing(self, candidate_id: str, slug: str) -> dict:
        self._require_client()
        candidate_id, slug = str(candidate_id), str(slug).strip()
        if not slug:
            return self._outcome(candidate_id, "", "not_verified", reason="A Mealie slug is required.")
        with _state_lock(self.review_path):
            events = self._read_events()
            state = StateStore(self.state_path)
            record = self._state_for(state, candidate_id)
            candidate_events = self._candidate_events(events, candidate_id)
            if not self._is_uncertain(candidate_events, record):
                return self._outcome(candidate_id, str((record or {}).get("title", "")),
                                     "not_uncertain", reason="This candidate has no unresolved create attempt.")
            self._append_locked({
                "event": "reconcile_started", "schema_version": 1, "event_id": str(uuid.uuid4()),
                "candidate_id": candidate_id, "slug": slug, "created_at": _now(),
            })
        try:
            mealie_recipe = self.client.get_recipe(slug)
        except Exception:
            return self._outcome(candidate_id, str((record or {}).get("title", "")),
                                 "not_verified", reason="Mealie could not verify that recipe identity.")
        if not mealie_recipe:
            with _state_lock(self.review_path):
                self._append_locked({
                    "event": "reconcile_not_found", "schema_version": 1,
                    "event_id": str(uuid.uuid4()), "candidate_id": candidate_id,
                    "slug": slug, "created_at": _now(),
                })
            return self._outcome(candidate_id, str((record or {}).get("title", "")),
                                 "not_proven_absent",
                                 reason="A lookup miss does not prove the recipe was not created.")
        title = str((record or {}).get("title", ""))
        if (not mealie_recipe.get("id") or mealie_recipe.get("slug") != slug
                or self._normalize(str(mealie_recipe.get("name", ""))) != self._normalize(title)):
            return self._outcome(candidate_id, title, "not_verified",
                                 reason="The selected slug returned a different recipe title.")
        with _state_lock(self.review_path):
            self._append_locked({
                "event": "reconciled_existing", "schema_version": 1,
                "event_id": str(uuid.uuid4()), "candidate_id": candidate_id,
                "slug": slug, "title": title, "source_hash": str((record or {}).get("source_hash", "")),
                "source_path": str((record or {}).get("file", "")), "created_at": _now(),
            })
        try:
            StateStore(self.state_path).add(
                candidate_id, "IMPORTED", slug, str((record or {}).get("file", "")), title,
                identity=candidate_id, source_hash=str((record or {}).get("source_hash", "")),
                creation_status="linked_existing", tag_status=str((record or {}).get("tag_status", "unknown")),
                batch_tag=str((record or {}).get("batch_tag", "")),
            )
        except Exception:
            return self._outcome(candidate_id, title, "linked_existing",
                                 reason="Existing recipe was verified; local compatibility history needs repair.",
                                 slug=slug)
        return self._outcome(candidate_id, title, "linked_existing", slug=slug)

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
        try:
            tag = tag_client.ensure_tag(tag_name)
            result = tag_client.add_tag(slug, tag)
            status = "already_tagged" if result == "already_present" else "tagged"
            event = {
                "event": "tagged", "schema_version": 1, "event_id": str(uuid.uuid4()),
                "attempt_id": attempt_id, "candidate_id": candidate_id, "title": title,
                "slug": slug, "batch_tag": tag_name, "created_at": _now(),
            }
        except Exception as exc:
            _, category, http_status = self._create_failure(exc)
            status = "failed"
            event = {
                "event": "tag_failed", "schema_version": 1, "event_id": str(uuid.uuid4()),
                "attempt_id": attempt_id, "candidate_id": candidate_id, "title": title,
                "slug": slug, "batch_tag": tag_name, "category": category,
                "http_status": http_status, "created_at": _now(),
            }
        with _state_lock(self.review_path):
            self._append_locked(event)
        try:
            state = StateStore(self.state_path)
            old = self._state_for(state, candidate_id) or {}
            state.add(
                candidate_id, "IMPORTED", slug, str(old.get("file", "")), title,
                identity=candidate_id, source_hash=str(old.get("source_hash", "")),
                extraction_id=str(old.get("extraction_id", "")),
                review_revision=old.get("review_revision"), content_sha256=str(old.get("content_sha256", "")),
                import_operation_id=str(old.get("import_operation_id", "")),
                creation_status=str(old.get("creation_status", "confirmed")),
                tag_status="tagged" if status in {"tagged", "already_tagged"} else "failed",
                batch_tag=tag_name,
            )
        except Exception:
            if status in {"tagged", "already_tagged"}:
                return {"status": "unknown", "reason": "Tag update succeeded but local history could not be updated."}
        return {
            "status": "tagged" if status in {"tagged", "already_tagged"} else "failed",
            "reason": "" if status in {"tagged", "already_tagged"}
            else self._failure_message(category, http_status),
        }

    def _eligibility(self, review: ReviewStore, events: list[dict],
                     view: RecipeView | None) -> tuple[str, dict | None]:
        if view is None:
            return "Candidate is no longer available.", None
        if view.revision < 1:
            return "UI import requires an explicitly approved review revision.", None
        if view.identity_status != "registered":
            return "Candidate identity needs reconciliation.", None
        if not view.source_path or not self._source_matches(view.source_path, view.source_hash):
            return "Source scan is unavailable or no longer matches.", None
        effective = self._effective(review, view)
        if effective is None or not effective.import_ready:
            return "Exact revision is no longer approved or has unresolved review items.", None
        if effective.revision != view.revision:
            return "Review changed while loading; refresh the import preflight.", None
        registration = self._registration(review, view)
        if not registration:
            return "Candidate registration is unavailable.", None
        if registration.get("identity_status") != "registered":
            return "Candidate identity needs reconciliation.", None
        try:
            protected = review._protected_import_outcome(registration)
        except ReviewConflict:
            return "Import history is unreadable; candidate remains protected.", None
        if protected:
            return "Candidate already has a protected import outcome.", None
        if self._service_protected(self._candidate_events(events, view.candidate_id)):
            status = self._service_protected_status(self._candidate_events(events, view.candidate_id))
            return ("A prior create attempt is uncertain; check Mealie before retrying."
                    if status == "uncertain" else "Candidate was already created; use tag repair if needed."), None
        return "", effective.recipe

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
