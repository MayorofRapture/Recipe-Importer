"""Append-only local review queue for extraction diagnostics and proposals."""
from __future__ import annotations

import csv
import json
import os
import re
import hashlib
import uuid
from collections import Counter
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from recipe_quality import RecipeCandidate, Suggestion, is_complete


class ReviewConflict(RuntimeError):
    """A stale revision or reused operation ID cannot be safely committed."""


@dataclass(frozen=True)
class EffectiveReview:
    candidate_id: str
    recipe: dict
    revision: int
    content_sha256: str
    approved: bool
    import_ready: bool
    resolutions: tuple[dict, ...] = ()
    legacy_draft_conflict: bool = False
    revision_target_bindings: tuple[dict, ...] = ()


@contextmanager
def _state_lock(path: Path):
    """Cross-process lock for short local JSONL transitions."""
    lock_path = path.parent / ".recipe-importer-state.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as handle:
        if os.fstat(handle.fileno()).st_size == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            deadline = time.monotonic() + 10
            while True:
                handle.seek(0)
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise ReviewConflict("another process is writing local recipe state")
                    time.sleep(0.05)
        else:
            import fcntl
            deadline = time.monotonic() + 10
            while True:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise ReviewConflict("another process is writing local recipe state")
                    time.sleep(0.05)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _canonical(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _recipe_digest(recipe: dict) -> str:
    fields = {key: recipe.get(key) for key in (
        "name", "ingredients", "instructions", "yield_text", "description",
        "prep_time", "perform_time", "total_time", "recipe_category", "tags",
    ) if key in recipe}
    return hashlib.sha256(b"recipe-content-v1\0" + _canonical(fields).encode("utf-8")).hexdigest()


def _resolution_digest(resolutions: list[dict]) -> str:
    values = [{key: value for key, value in item.items()
               if key not in {"event_id", "timestamp", "actor"}}
              for item in resolutions]
    values.sort(key=lambda item: (item.get("issue_id", ""), item.get("target_id", ""),
                                  item.get("operation_id", "")))
    return hashlib.sha256(_canonical(values).encode("utf-8")).hexdigest()


def extraction_identity(source_hash: str, extraction: dict) -> str:
    return hashlib.sha256((source_hash + _canonical(extraction)).encode("utf-8")).hexdigest()


def _legacy_identity(source_hash: str, title: str) -> str:
    normalized = re.sub(r"\s+", " ", title.strip().casefold())
    return f"{source_hash}:{hashlib.sha256(normalized.encode('utf-8')).hexdigest()[:20]}"


def _mapped_issue_target(issue: dict, revision: dict) -> dict | None:
    source_target_id = issue.get("source_target_id")
    if not source_target_id:
        return None
    targets = [item for item in revision.get("target_bindings", [])
               if item.get("target_id") == source_target_id
               and item.get("field") == issue.get("field")]
    if len(targets) != 1:
        return None
    target = targets[0]
    field = target.get("field")
    if issue.get("source_target_kind") == "row":
        values = revision.get("recipe", {}).get(field)
        try:
            position = int(target.get("position", -1))
        except (TypeError, ValueError):
            return None
        if not isinstance(values, list) or position < 0 or position >= len(values):
            return None
    elif issue.get("source_target_kind") != "field" or field not in {
        "name", "ingredients", "instructions", "yield_text", "description",
    }:
        return None
    return target


@dataclass(frozen=True)
class ReviewDecision:
    suggestion_id: str
    status: str
    final_value: Optional[str]
    timestamp: str = ""


class ReviewStore:
    def __init__(self, path: Path, *, state_path: Path | None = None):
        self.path = path
        self.state_path = Path(state_path) if state_path is not None else self.path.parent / "import_state.jsonl"
        self.lock = None
        self.candidates: dict[str, dict] = {}
        self.decisions: dict[str, list[ReviewDecision]] = {}
        self.candidate_decisions: dict[str, list[ReviewDecision]] = {}
        with _state_lock(path):
            self._reload()

    def _reload(self) -> None:
        self.candidates.clear()
        self.decisions.clear()
        self.candidate_decisions.clear()
        self.registrations = {}
        self.revisions = {}
        self.approvals = {}
        self.resolutions = {}
        self.events = []
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                event = json.loads(line)
                self.events.append(event)
                identity = event.get("candidate_id", "")
                kind = event.get("event")
                if kind == "candidate_registration":
                    self.registrations.setdefault(identity, []).append(event)
                elif kind == "review_revision":
                    self.revisions.setdefault(identity, []).append(event)
                elif kind == "review_approval":
                    self.approvals.setdefault(identity, []).append(event)
                elif kind == "review_issue_resolution":
                    self.resolutions.setdefault(identity, []).append(event)
                elif kind == "candidate":
                    self.candidates[identity] = event
                elif event.get("event") == "decision":
                    self.decisions.setdefault(identity, []).append(ReviewDecision(
                        event["suggestion_id"], event["status"], event.get("final_value"), event.get("timestamp", "")
                    ))
                elif event.get("event") == "candidate_decision":
                    self.candidate_decisions.setdefault(identity, []).append(ReviewDecision(
                        "", event["status"], event.get("final_value"), event.get("timestamp", "")
                    ))

    def _append(self, event: dict, *, locked: bool = False) -> None:
        if not locked:
            with _state_lock(self.path):
                self._reload()
                self._append(event, locked=True)
                self._reload()
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        self.events.append(event)

    def _readback(self, event: dict) -> dict:
        readback = next((item for item in self.events if item.get("event_id") == event["event_id"]), None)
        if readback is None or _canonical(readback) != _canonical(event):
            raise ReviewConflict("durable review event readback did not match the appended event")
        return readback

    def _protected_import_outcome(self, registration: dict) -> dict | None:
        from import_protection import ImportProtectionError, protected_candidate_ids

        state_path = self.state_path
        aliases = {registration.get("candidate_id", ""), *registration.get("legacy_aliases", [])}
        if registration.get("legacy_id"):
            aliases.add(registration["legacy_id"])
        source_hash = registration.get("source_hash", "")
        title = re.sub(r"\s+", " ", str(registration.get("candidate", {}).get("name", "")).strip().casefold())
        duplicate_registered_title = len({
            item.get("candidate_id")
            for events in self.registrations.values()
            for item in events
            if item.get("source_hash") == source_hash
            and item.get("extraction_id") == registration.get("extraction_id")
            and re.sub(r"\s+", " ", str(item.get("candidate", {}).get("name", "")).strip().casefold()) == title
        }) > 1
        try:
            records = ([json.loads(line) for line in state_path.read_text(encoding="utf-8").splitlines()
                        if line.strip()] if state_path.exists() else [])
        except (OSError, json.JSONDecodeError) as exc:
            raise ReviewConflict("import state is unreadable; approval is protected pending reconciliation") from exc
        for record in records:
            if record.get("status") not in {"IMPORTED", "UNCERTAIN"}:
                continue
            if record.get("identity") in aliases or record.get("key") in aliases:
                return record
            key = str(record.get("key", ""))
            record_title = re.sub(r"\s+", " ", str(record.get("title", "")).strip().casefold())
            record_identity = str(record.get("identity", ""))
            ambiguous_legacy_identity = record_identity == _legacy_identity(source_hash, title) if title else False
            if (source_hash and key.startswith(source_hash + ":") and title and record_title == title
                    and (not duplicate_registered_title or not record_identity or ambiguous_legacy_identity)):
                return record
        try:
            protected = protected_candidate_ids(self.path.parent, state_path=state_path)
        except ImportProtectionError as exc:
            raise ReviewConflict(str(exc)) from exc
        if aliases.intersection(protected):
            return {"status": "UNCERTAIN", "identity": registration.get("candidate_id", ""),
                    "protection_source": "durable_import_intent"}
        return None

    def _base_event(self, event: str, candidate_id: str, source_hash: str,
                    extraction_id: str, operation_id: str) -> dict:
        if not operation_id.strip():
            raise ValueError("operation_id must not be empty")
        return {
            "event": event, "schema_version": 1, "event_id": str(uuid.uuid4()),
            "operation_id": operation_id, "candidate_id": candidate_id,
            "source_hash": source_hash, "extraction_id": extraction_id,
            "timestamp": datetime.now(timezone.utc).isoformat(), "actor": "local-user",
        }

    def canonical_candidate_id(self, candidate_id: str) -> str:
        for registered, events in self.registrations.items():
            if candidate_id == registered or any(candidate_id in event.get("legacy_aliases", [])
                                                   or candidate_id == event.get("title_legacy_id")
                                                   for event in events):
                return registered
        return candidate_id

    def _existing_operation(self, operation_id: str, event: str, expected: dict) -> dict | None:
        for prior in self.events:
            if prior.get("operation_id") == operation_id:
                comparable = {k: v for k, v in prior.items()
                              if k not in {"event_id", "timestamp", "actor"}}
                if comparable != expected:
                    raise ReviewConflict("operation_id was already used for different review data")
                return prior
        return None

    def register_candidates(self, source: str, source_hash: str, extraction_id: str,
                            candidates: list[RecipeCandidate]) -> list[dict]:
        exact = self._exact_registration_batch(source_hash, extraction_id, candidates)
        if exact is not None:
            # Catalog reads commonly revisit every source in an already
            # registered library. Registrations are immutable, so reuse this
            # loaded snapshot; any missing identity still takes the locked,
            # fresh-read mutation path below.
            return exact
        with _state_lock(self.path):
            self._reload()
            prior_source_registration = any(
                event.get("source_hash") == source_hash
                for events in self.registrations.values() for event in events
            )
            source_import_protected = self._source_has_protected_import_history(source_hash)
            fingerprints = [hashlib.sha256(_canonical(item.model_dump(mode="json")).encode()).hexdigest()
                            for item in candidates]
            current_payloads = {_canonical(item.model_dump(mode="json")) for item in candidates}
            prior_source_legacy_candidate = any(
                event.get("event") == "candidate"
                and (str(event.get("candidate_id", "")).startswith(source_hash + ":")
                     or str(event.get("source", "")).casefold() == str(source).casefold())
                and _canonical(event.get("candidate", {})) not in current_payloads
                for event in self.events
            )
            has_prior_source = (prior_source_registration or prior_source_legacy_candidate
                                or source_import_protected)
            repeated = {value for value, count in Counter(fingerprints).items() if count > 1}
            titles = [item.name.casefold().strip() for item in candidates]
            title_counts = Counter(titles)
            exact_index: dict[str, list[dict]] = {}
            prior_index: dict[str, list[dict]] = {}
            for events in self.registrations.values():
                for event in events:
                    if event.get("source_hash") != source_hash:
                        continue
                    if event.get("extraction_id") == extraction_id:
                        exact_index.setdefault(event["fingerprint"], []).append(event)
                    if event.get("identity_status") == "registered":
                        prior_index.setdefault(event["fingerprint"], []).append(event)
            legacy_index: dict[str, list[str]] = {}
            for event in self.events:
                identity = str(event.get("candidate_id", ""))
                same_source = identity.startswith(source_hash + ":") or any(
                    item.get("source_hash") == source_hash
                    for item in self.registrations.get(identity, []))
                if not same_source and ":" not in identity and source:
                    same_source = str(event.get("source", "")).casefold() == str(source).casefold()
                if event.get("event") == "candidate" and same_source:
                    legacy_index.setdefault(_canonical(event.get("candidate", {})), []).append(identity)
            occurrences: dict[str, int] = {}
            output = []
            for candidate, fingerprint in zip(candidates, fingerprints):
                occurrence = occurrences.get(fingerprint, 0)
                occurrences[fingerprint] = occurrence + 1
                exact = exact_index.get(fingerprint, [])
                if fingerprint in repeated:
                    # Identical siblings have no defensible identity. Keep each
                    # persisted registration stable, but block all of them.
                    if occurrence < len(exact):
                        output.append(exact[occurrence])
                        continue
                    candidate_id, identity_status = str(uuid.uuid4()), "needs_reconciliation"
                elif exact:
                    output.append(exact[0])
                    continue
                prior = prior_index.get(fingerprint, [])
                if fingerprint not in repeated and len({event["candidate_id"] for event in prior}) == 1:
                    candidate_id, identity_status = prior[0]["candidate_id"], "registered"
                elif fingerprint not in repeated and prior:
                    candidate_id, identity_status = str(uuid.uuid4()), "needs_reconciliation"
                elif fingerprint not in repeated:
                    legacy_matches = legacy_index.get(_canonical(candidate.model_dump(mode="json")), [])
                    duplicate_title = title_counts[candidate.name.casefold().strip()] > 1
                    if duplicate_title and legacy_matches:
                        candidate_id, identity_status = str(uuid.uuid4()), "needs_reconciliation"
                    elif has_prior_source and (len(legacy_matches) != 1 or duplicate_title):
                        candidate_id, identity_status = str(uuid.uuid4()), "needs_reconciliation"
                    else:
                        candidate_id = (legacy_matches[0] if len(legacy_matches) == 1 and not duplicate_title
                                        else str(uuid.uuid4()))
                        identity_status = "registered"
                bindings = []
                for field_name in ("ingredients", "instructions"):
                    for position, value in enumerate(getattr(candidate, field_name)):
                        bindings.append({"target_id": str(uuid.uuid4()), "target_kind": "row",
                                          "field": field_name, "original_value": value,
                                          "original_position": position})

                field_targets: dict[str, dict] = {}

                def target_for_path(field_path: str) -> tuple[str, str]:
                    match = re.fullmatch(r"(ingredients|instructions)\[(\d+)\]", field_path)
                    if match:
                        row = next((item for item in bindings
                                    if item.get("target_kind") == "row"
                                    and item.get("field") == match.group(1)
                                    and item.get("original_position") == int(match.group(2))), None)
                        return (str(row["target_id"]), "row") if row else ("", "")
                    if field_path not in {"name", "ingredients", "instructions", "yield_text", "description"}:
                        return "", ""
                    target = field_targets.get(field_path)
                    if target is None:
                        target = {"target_id": str(uuid.uuid4()), "target_kind": "field",
                                  "field": field_path,
                                  "original_value": getattr(candidate, field_path)}
                        bindings.append(target)
                        field_targets[field_path] = target
                    return str(target["target_id"]), "field"

                for suggestion in candidate.suggestions:
                    source_target, source_target_kind = target_for_path(suggestion.field_path)
                    bindings.append({"issue_id": suggestion.suggestion_id,
                                     "target_id": str(uuid.uuid4()), "target_kind": "proposal",
                                     "field_path_at_extraction": suggestion.field_path,
                                     "source_target_id": source_target,
                                     "source_target_kind": source_target_kind,
                                     "field": (suggestion.field_path.split("[", 1)[0]
                                               if source_target_kind == "row" else
                                               suggestion.field_path),
                                     "original_value": suggestion.proposed_value,
                                     "original_issue": suggestion.model_dump(mode="json")})
                for finding_index, finding in enumerate(candidate.missing_information):
                    source_target, source_target_kind = target_for_path(finding.field_path)
                    bindings.append({"issue_id": f"finding:{finding_index}:{finding.field_path}",
                                     "target_id": str(uuid.uuid4()), "target_kind": "finding",
                                     "field_path_at_extraction": finding.field_path,
                                     "severity": finding.severity, "source_target_id": source_target,
                                     "source_target_kind": source_target_kind,
                                     "field": (finding.field_path.split("[", 1)[0]
                                               if source_target_kind == "row" else finding.field_path),
                                     "original_value": finding.detail,
                                     "original_issue": finding.model_dump(mode="json")})
                event = self._base_event("candidate_registration", candidate_id, source_hash,
                                         extraction_id,
                                         f"register:{extraction_id}:{fingerprint}:"
                                         f"{candidate_id if fingerprint in repeated else 'unique'}")
                title_legacy_id = (_legacy_identity(source_hash, candidate.name)
                                   if title_counts[candidate.name.casefold().strip()] == 1 else "")
                event.update({"source": source, "fingerprint": fingerprint,
                              "identity_status": identity_status,
                              "legacy_id": f"{source_hash}:{fingerprint[:24]}",
                              "title_legacy_id": title_legacy_id,
                              "legacy_aliases": [f"{source_hash}:{fingerprint[:24]}",
                                                 *([title_legacy_id] if title_legacy_id else [])],
                              "candidate": candidate.model_dump(mode="json"),
                              "target_bindings": bindings})
                # Registration retries are keyed by immutable extraction/fingerprint.
                existing = next((e for e in self.events if e.get("event") == event["event"]
                                 and e.get("operation_id") == event["operation_id"]), None)
                if existing:
                    output.append(existing)
                    continue
                self._append(event, locked=True)
                self.registrations.setdefault(candidate_id, []).append(event)
                exact_index.setdefault(fingerprint, []).append(event)
                if identity_status == "registered":
                    prior_index.setdefault(fingerprint, []).append(event)
                output.append(event)
            return output

    def _exact_registration_batch(self, source_hash: str, extraction_id: str,
                                  candidates: list[RecipeCandidate]) -> list[dict] | None:
        """Return stable registrations from this snapshot, or require a fresh locked read."""
        exact_index: dict[str, list[dict]] = {}
        for events in self.registrations.values():
            for event in events:
                if (event.get("source_hash") == source_hash
                        and event.get("extraction_id") == extraction_id):
                    exact_index.setdefault(event.get("fingerprint", ""), []).append(event)
        occurrences: dict[str, int] = {}
        output = []
        for candidate in candidates:
            fingerprint = hashlib.sha256(_canonical(candidate.model_dump(mode="json")).encode()).hexdigest()
            occurrence = occurrences.get(fingerprint, 0)
            occurrences[fingerprint] = occurrence + 1
            matches = exact_index.get(fingerprint, [])
            if (occurrence != 0 or len(matches) != 1
                    or matches[0].get("identity_status") != "registered"):
                return None
            output.append(matches[0])
        return output

    def _source_has_protected_import_history(self, source_hash: str) -> bool:
        if not source_hash:
            return False
        try:
            state_records = ([json.loads(line) for line in self.state_path.read_text(encoding="utf-8").splitlines()
                              if line.strip()] if self.state_path.exists() else [])
        except (OSError, json.JSONDecodeError) as exc:
            raise ReviewConflict("import state is unreadable; new identities remain held") from exc
        for record in state_records:
            if not isinstance(record, dict) or record.get("status") not in {
                "IMPORTED", "UNCERTAIN", "NEEDS_MANUAL_CHECK",
            }:
                continue
            if (record.get("source_hash") == source_hash
                    or str(record.get("key", "")).startswith(source_hash + ":")):
                return True

        event_path = self.path.parent / "ui_import_events.jsonl"
        try:
            events = ([json.loads(line) for line in event_path.read_text(encoding="utf-8").splitlines()
                       if line.strip()] if event_path.exists() else [])
        except (OSError, json.JSONDecodeError) as exc:
            raise ReviewConflict("import history is unreadable; new identities remain held") from exc
        attempts: dict[str, str] = {}
        for event in events:
            if not isinstance(event, dict) or event.get("source_hash") != source_hash:
                continue
            attempt_id = str(event.get("attempt_id", ""))
            kind = event.get("event")
            if kind == "intent_started" and attempt_id:
                attempts[attempt_id] = "started"
            elif kind == "create_rejected" and attempt_id:
                attempts[attempt_id] = "rejected"
            elif kind in {"create_uncertain", "creation_confirmed", "reconciled_existing"}:
                return True
        return "started" in attempts.values()

    def record_reconciliation(self, *, candidate_id: str, source_hash: str,
                              extraction_id: str, attempt_id: str,
                              new_candidate_id: str, new_source_hash: str,
                              new_extraction_id: str, target_candidate_id: str | None,
                              decision: str, sections: list[str], evidence: str,
                              revision: int, operation_id: str) -> dict:
        if decision not in {"mapped", "declared_new", "deferred"}:
            raise ValueError("Unsupported reconciliation decision")
        if not evidence.strip():
            raise ValueError("Reconciliation evidence is required")
        event = self._base_event("candidate_reconciliation", candidate_id, source_hash,
                                 extraction_id, operation_id)
        event.update({"attempt_id": attempt_id, "new_candidate_id": new_candidate_id,
                      "new_source_hash": new_source_hash,
                      "new_extraction_id": new_extraction_id,
                      "target_candidate_id": target_candidate_id,
                      "decision": decision, "sections": list(sections),
                      "evidence": evidence.strip(), "revision": int(revision)})
        expected = {key: value for key, value in event.items()
                    if key not in {"event_id", "timestamp", "actor"}}
        with _state_lock(self.path):
            self._reload()
            prior = self._existing_operation(operation_id, "candidate_reconciliation", expected)
            if prior:
                return prior
            self._append(event, locked=True)
            self._reload()
            return self._readback(event)

    def register_reconciled_candidate(self, *, candidate_id: str, source_hash: str,
                                      extraction_id: str, source: str,
                                      candidate: RecipeCandidate,
                                      target_bindings: list[dict], attempt_id: str,
                                      origin_candidate_id: str) -> dict:
        """Add an explicitly declared new attempt candidate without changing caches."""
        payload = candidate.model_dump(mode="json")
        fingerprint = hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()
        operation_id = f"reconciled-register:{attempt_id}:{candidate_id}"
        event = self._base_event("candidate_registration", candidate_id, source_hash,
                                 extraction_id, operation_id)
        event.update({"source": source, "fingerprint": fingerprint,
                      "identity_status": "registered", "legacy_id": "",
                      "title_legacy_id": "", "legacy_aliases": [],
                      "candidate": payload,
                      "target_bindings": json.loads(json.dumps(target_bindings, ensure_ascii=False)),
                      "reconciled_from": {"attempt_id": attempt_id,
                                          "candidate_id": origin_candidate_id}})
        expected = {key: value for key, value in event.items()
                    if key not in {"event_id", "timestamp", "actor"}}
        with _state_lock(self.path):
            self._reload()
            prior = self._existing_operation(operation_id, "candidate_registration", expected)
            if prior:
                return prior
            if self.registrations.get(candidate_id):
                raise ReviewConflict("candidate identity is already registered; explicit reconciliation is required")
            if any(item.get("source_hash") == source_hash
                   and item.get("extraction_id") == extraction_id
                   and item.get("fingerprint") == fingerprint
                   for events in self.registrations.values() for item in events):
                raise ReviewConflict("this extraction candidate is already registered under another identity")
            self._append(event, locked=True)
            self._reload()
            return self._readback(event)

    def save_revision(self, *, candidate_id: str, source_hash: str, extraction_id: str,
                      expected_parent: int, operation_id: str, recipe: dict,
                      target_bindings: list[dict], raw_bulk_text: dict | None = None,
                      mode: str = "structured", warnings: list[str] | None = None) -> dict:
        digest = _recipe_digest(recipe)
        with _state_lock(self.path):
            self._reload()
            registration = next((item for item in self.registrations.get(candidate_id, [])
                                 if item.get("source_hash") == source_hash
                                 and item.get("extraction_id") == extraction_id), None)
            if not registration:
                raise ReviewConflict("candidate must be registered before saving a review revision")
            revision_targets = json.loads(json.dumps(target_bindings, ensure_ascii=False))
            for source_target in registration.get("target_bindings", []):
                if source_target.get("target_kind") != "field":
                    continue
                matches = [item for item in revision_targets
                           if item.get("target_id") == source_target.get("target_id")]
                if matches and any(item.get("field") != source_target.get("field") for item in matches):
                    raise ReviewConflict("review field binding conflicts with its immutable source target")
                if not matches:
                    revision_targets.append({key: source_target[key] for key in (
                        "target_id", "target_kind", "field", "original_value",
                    )})
            event = self._base_event("review_revision", candidate_id, source_hash,
                                     extraction_id, operation_id)
            event.update({"revision": expected_parent + 1, "parent_revision": expected_parent,
                          "recipe": recipe, "target_bindings": revision_targets,
                          "raw_bulk_text": raw_bulk_text or {"ingredients": "", "instructions": ""},
                          "mode": mode, "warnings": list(warnings or []),
                          "content_sha256": digest, "content_digest_version": 1})
            expected = {k: v for k, v in event.items()
                        if k not in {"event_id", "timestamp", "actor"}}
            prior = self._existing_operation(operation_id, "review_revision", expected)
            if prior:
                return prior
            revisions = [item for item in self.revisions.get(candidate_id, [])
                         if item.get("source_hash") == source_hash
                         and item.get("extraction_id") == extraction_id]
            current = max((int(item["revision"]) for item in revisions), default=0)
            if current != expected_parent:
                raise ReviewConflict(f"revision changed: expected {expected_parent}, current {current}")
            self._append(event, locked=True)
            self._reload()
            return self._readback(event)

    def resolve_issue(self, *, candidate_id: str, source_hash: str, extraction_id: str,
                      revision: int, issue_id: str, target_id: str, disposition: str,
                      final_value: str | None, operation_id: str,
                      evidence_reference: str = "") -> dict:
        if disposition not in {"source_corrected", "human_completion", "not_recipe_content",
                               "accepted", "edited", "rejected", "deferred"}:
            raise ValueError(f"Unsupported issue disposition: {disposition}")
        with _state_lock(self.path):
            self._reload()
            registration = next((item for item in self.registrations.get(candidate_id, [])
                                 if item.get("extraction_id") == extraction_id), None)
            if not registration or registration.get("source_hash") != source_hash:
                raise ReviewConflict("issue resolution source hash does not match its registration")
            issue = next((item for item in registration["target_bindings"]
                          if item.get("issue_id") == issue_id
                          and item.get("target_id") == target_id), None)
            if not issue:
                raise ReviewConflict("issue target is not registered for this extraction")
            if disposition == "not_recipe_content" and not evidence_reference.strip():
                raise ValueError("not_recipe_content requires an evidence reference")
            if (disposition in {"source_corrected", "human_completion", "accepted", "edited"}
                    and not issue.get("source_target_id")):
                raise ReviewConflict("issue has no uniquely mapped immutable recipe target")
            event = self._base_event("review_issue_resolution", candidate_id, source_hash,
                                     extraction_id, operation_id)
            event.update({"revision": revision, "issue_id": issue_id, "target_id": target_id,
                          "disposition": disposition, "final_value": final_value,
                          "evidence_reference": evidence_reference,
                          "original_issue": json.loads(json.dumps(issue, ensure_ascii=False))})
            expected = {k: v for k, v in event.items()
                        if k not in {"event_id", "timestamp", "actor"}}
            prior = self._existing_operation(operation_id, "review_issue_resolution", expected)
            if prior:
                return prior
            if any(item.get("event") == "review_issue_resolution"
                   and item.get("candidate_id") == candidate_id
                   and item.get("source_hash") == source_hash
                   and item.get("extraction_id") == extraction_id
                   and item.get("revision") == revision
                   and item.get("issue_id") == issue_id
                   and item.get("target_id") == target_id for item in self.events):
                raise ReviewConflict("issue already has a resolution for this revision; save a new revision to change it")
            current = max((int(item["revision"]) for item in self.revisions.get(candidate_id, [])
                           if item.get("source_hash") == source_hash
                           and item.get("extraction_id") == extraction_id), default=0)
            if revision != current:
                raise ReviewConflict(f"revision changed: expected {revision}, current {current}")
            self._append(event, locked=True)
            self._reload()
            return self._readback(event)

    def approve_revision(self, *, candidate_id: str, source_hash: str, extraction_id: str,
                         revision: int, operation_id: str, source_path: str | Path) -> dict:
        with _state_lock(self.path):
            self._reload()
            if not any(item.get("source_hash") == source_hash
                       and item.get("extraction_id") == extraction_id
                       for item in self.registrations.get(candidate_id, [])):
                raise ReviewConflict("candidate must be registered before approval")
            snapshot = next((item for item in self.revisions.get(candidate_id, [])
                             if int(item["revision"]) == revision
                             and item["source_hash"] == source_hash
                             and item["extraction_id"] == extraction_id), None)
            if not snapshot:
                raise ReviewConflict("the exact review revision is not present")
            recipe = RecipeCandidate.model_validate(snapshot["recipe"])
            if not is_complete(recipe):
                raise ReviewConflict("an empty or incomplete recipe cannot be approved")
            if (snapshot.get("content_digest_version") != 1
                    or _recipe_digest(snapshot["recipe"]) != snapshot.get("content_sha256")):
                raise ReviewConflict("review revision content digest does not match its recipe snapshot")
            registration = next((item for item in self.registrations.get(candidate_id, [])
                                 if item.get("extraction_id") == extraction_id), None)
            if not registration or registration.get("source_hash") != source_hash:
                raise ReviewConflict("approval source hash does not match its registration")
            if registration and registration.get("identity_status") != "registered":
                raise ReviewConflict("candidate identity requires explicit reconciliation")
            resolutions = [item for item in self.resolutions.get(candidate_id, [])
                           if item.get("source_hash") == source_hash
                           and item.get("extraction_id") == extraction_id
                           and item.get("revision") == revision]
            if registration:
                pending = []
                for item in registration["target_bindings"]:
                    kind = item.get("target_kind")
                    if kind not in {"proposal", "finding"} or (kind == "finding" and item.get("severity") != "critical"):
                        continue
                    valid = {"source_corrected", "human_completion", "not_recipe_content"} if kind == "finding" else {
                        "source_corrected", "human_completion", "not_recipe_content", "accepted", "edited"
                    }
                    if not any(res.get("issue_id") == item.get("issue_id")
                               and res.get("revision") == revision and res.get("disposition") in valid
                               for res in resolutions):
                        pending.append(item)
                if pending:
                    raise ReviewConflict("pending proposals must be resolved before approval")
                for resolution in resolutions:
                    issue = next((item for item in registration["target_bindings"]
                                  if item.get("issue_id") == resolution.get("issue_id")), None)
                    if (issue and issue.get("source_target_id")
                            and resolution.get("disposition") in {
                                "source_corrected", "human_completion", "accepted", "edited",
                            }
                            and not _mapped_issue_target(issue, snapshot)):
                        raise ReviewConflict("resolved source target is not present in this recipe revision")
            event = self._base_event("review_approval", candidate_id, source_hash,
                                     extraction_id, operation_id)
            event.update({"revision": revision, "content_sha256": snapshot["content_sha256"],
                          "content_digest_version": snapshot.get("content_digest_version", 1),
                          "source_path": str(source_path),
                          "resolution_event_ids": [item["event_id"] for item in resolutions],
                          "resolution_sha256": _resolution_digest(resolutions)})
            expected = {k: v for k, v in event.items()
                        if k not in {"event_id", "timestamp", "actor"}}
            prior = self._existing_operation(operation_id, "review_approval", expected)
            if prior:
                return prior
            if self._protected_import_outcome(registration):
                raise ReviewConflict("candidate has a protected import outcome and cannot receive new approval")
            try:
                actual_source_hash = hashlib.sha256(Path(source_path).read_bytes()).hexdigest()
            except OSError as exc:
                raise ReviewConflict("source scan is unavailable; no new approval was recorded") from exc
            if actual_source_hash != source_hash:
                raise ReviewConflict("source scan bytes do not match the registered source hash")
            current = max((int(item["revision"]) for item in self.revisions.get(candidate_id, [])
                           if item.get("source_hash") == source_hash
                           and item.get("extraction_id") == extraction_id), default=0)
            if revision != current:
                raise ReviewConflict(f"revision changed: expected {revision}, current {current}")
            self._append(event, locked=True)
            self._reload()
            return self._readback(event)

    def project(self, candidate_id: str, source_hash: str, extraction_id: str,
                legacy_candidate: RecipeCandidate, legacy_draft=None) -> EffectiveReview:
        with _state_lock(self.path):
            self._reload()
            return self.project_loaded(candidate_id, source_hash, extraction_id,
                                       legacy_candidate, legacy_draft)

    def project_loaded(self, candidate_id: str, source_hash: str, extraction_id: str,
                       legacy_candidate: RecipeCandidate, legacy_draft=None) -> EffectiveReview:
        """Project an already reloaded snapshot; caller owns its read/write boundary."""
        revisions = [item for item in self.revisions.get(candidate_id, [])
                     if item.get("source_hash") == source_hash
                     and item.get("extraction_id") == extraction_id]
        if not revisions:
            return EffectiveReview(candidate_id, legacy_candidate.model_dump(mode="json"), 0, "",
                                   False, False, ())
        latest = max(revisions, key=lambda item: int(item["revision"]))
        registration = next((item for item in self.registrations.get(candidate_id, [])
                             if item.get("extraction_id") == extraction_id), None)
        current_resolutions = [item for item in self.resolutions.get(candidate_id, [])
                               if item.get("source_hash") == source_hash
                               and item.get("extraction_id") == extraction_id
                               and item.get("revision") == latest["revision"]]
        required_resolution_ids = {item["event_id"] for item in current_resolutions}
        resolution_sha256 = _resolution_digest(current_resolutions)
        approvals = [item for item in self.approvals.get(candidate_id, [])
                     if item.get("revision") == latest["revision"]
                     and item.get("content_sha256") == latest["content_sha256"]
                     and item.get("extraction_id") == extraction_id
                     and required_resolution_ids == set(item.get("resolution_event_ids", []))
                     and item.get("resolution_sha256") == resolution_sha256]
        approval = max(approvals, key=lambda item: item.get("timestamp", ""), default=None)
        snapshot_integrity_valid = (
            latest.get("content_digest_version") == 1
            and _recipe_digest(latest.get("recipe", {})) == latest.get("content_sha256")
        )
        legacy_draft_conflict = bool(
            approval and legacy_draft and legacy_draft.saved_at
            and legacy_draft.saved_at > approval.get("timestamp", "")
        )
        approved = bool(approval and snapshot_integrity_valid and not legacy_draft_conflict and registration
                        and registration.get("identity_status") == "registered")
        recipe = latest["recipe"]
        recipe = json.loads(json.dumps(recipe, ensure_ascii=False))
        try:
            complete = is_complete(RecipeCandidate.model_validate(recipe))
        except (TypeError, ValueError):
            complete = False
        resolutions = tuple(item for item in self.resolutions.get(candidate_id, [])
                            if item.get("extraction_id") == extraction_id
                            and item.get("revision") == latest["revision"])
        if registration:
            for resolution in resolutions:
                issue = next((item for item in registration["target_bindings"]
                              if item.get("issue_id") == resolution.get("issue_id")), None)
                if not issue:
                    continue
                disposition = resolution.get("disposition")
                target = _mapped_issue_target(issue, latest)
                if (target and disposition in {"accepted", "edited", "source_corrected", "human_completion"}
                        and resolution.get("final_value") is not None):
                    if issue.get("source_target_kind") == "row":
                        values = recipe.get(target["field"], [])
                        values[int(target["position"])] = resolution["final_value"]
                    else:
                        candidate = RecipeCandidate.model_validate(recipe)
                        _set_field(candidate, target["field"], str(resolution["final_value"]))
                        recipe = candidate.model_dump(mode="json")
                if disposition in {"accepted", "edited", "rejected", "not_recipe_content"}:
                    for suggestion in recipe.get("suggestions", []):
                        if suggestion.get("suggestion_id") == resolution.get("issue_id"):
                            suggestion["review_status"] = {
                                "accepted": "approved", "edited": "edited",
                                "rejected": "rejected", "not_recipe_content": "rejected",
                            }[disposition]
            resolved_findings = {item.get("issue_id") for item in registration["target_bindings"]
                                 if item.get("target_kind") == "finding"
                                 and any(res.get("issue_id") == item.get("issue_id")
                                         and res.get("disposition") in {"source_corrected", "human_completion", "not_recipe_content"}
                                         for res in resolutions)}
            recipe["missing_information"] = [item for item in recipe.get("missing_information", [])
                                              if not any(issue_id.endswith(":" + item.get("field_path", ""))
                                                         for issue_id in resolved_findings)]
        pending = bool(registration and any(
            item.get("target_kind") in {"proposal", "finding"}
            and (item.get("target_kind") != "finding" or item.get("severity") == "critical")
            and not any(res.get("issue_id") == item.get("issue_id")
                        and res.get("revision") == latest["revision"]
                        and res.get("disposition") in (
                            {"source_corrected", "human_completion", "not_recipe_content"}
                            if item.get("target_kind") == "finding" else
                            {"source_corrected", "human_completion", "not_recipe_content", "accepted", "edited"}
                        ) for res in resolutions)
            for item in registration["target_bindings"]))
        unmapped_resolution = bool(registration and any(
            resolution.get("disposition") in {"source_corrected", "human_completion", "accepted", "edited"}
            and not _mapped_issue_target(issue, latest)
            for resolution in resolutions
            for issue in registration["target_bindings"]
            if issue.get("issue_id") == resolution.get("issue_id")))
        import_ready = approved and complete and not pending and not unmapped_resolution
        if import_ready:
            recipe["requires_review"] = False
        return EffectiveReview(candidate_id, recipe, int(latest["revision"]),
                               latest["content_sha256"], approved,
                               import_ready, resolutions, legacy_draft_conflict,
                               tuple(latest.get("target_bindings", [])))

    def add_candidate(self, source: str, candidate_id: str, candidate: RecipeCandidate) -> None:
        if candidate_id in self.candidates:
            return
        event = {
            "event": "candidate",
            "source": source,
            "candidate_id": candidate_id,
            "candidate": candidate.model_dump(mode="json"),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        self._append(event)
        self.candidates[candidate_id] = event

    def record_candidate_decision(self, source: str, candidate_id: str,
                                 status: str, final_value: Optional[str] = None) -> None:
        if status not in {"pending", "approved", "edited", "rejected"}:
            raise ValueError(f"Unsupported review status: {status}")
        if status == "edited" and not (final_value or "").strip():
            raise ValueError("An edited candidate requires a JSON object in final_value")
        candidate_id = self.canonical_candidate_id(candidate_id)
        event = {
            "event": "candidate_decision", "source": source,
            "candidate_id": candidate_id, "status": status,
            "final_value": final_value,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        self._append(event)
        self.candidate_decisions.setdefault(candidate_id, []).append(
            ReviewDecision("", status, final_value, event["timestamp"])
        )

    def record(self, source: str, candidate_id: str, candidate: RecipeCandidate,
               suggestion_id: str, status: str, final_value: Optional[str] = None) -> None:
        if status not in {"pending", "approved", "edited", "rejected"}:
            raise ValueError(f"Unsupported review status: {status}")
        candidate_id = self.canonical_candidate_id(candidate_id)
        if not any(item.suggestion_id == suggestion_id for item in candidate.suggestions):
            raise ValueError(f"Unknown suggestion: {suggestion_id}")
        if candidate_id not in self.candidates:
            self.add_candidate(source, candidate_id, candidate)
        event = {
            "event": "decision",
            "source": source,
            "candidate_id": candidate_id,
            "suggestion_id": suggestion_id,
            "status": status,
            "final_value": final_value,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        self._append(event)
        self.decisions.setdefault(candidate_id, []).append(
            ReviewDecision(suggestion_id, status, final_value, event["timestamp"])
        )

    def decisions_for(self, candidate_id: str) -> list[ReviewDecision]:
        with _state_lock(self.path):
            self._reload()
        canonical = self.canonical_candidate_id(candidate_id)
        return [decision for key, decisions in self.decisions.items()
                if self.canonical_candidate_id(key) == canonical for decision in decisions]

    def candidate_decision(self, candidate_id: str) -> Optional[ReviewDecision]:
        with _state_lock(self.path):
            self._reload()
        canonical = self.canonical_candidate_id(candidate_id)
        decisions = [decision for key, values in self.candidate_decisions.items()
                     if self.canonical_candidate_id(key) == canonical for decision in values]
        return decisions[-1] if decisions else None

    def write_csv(self, path: Path) -> None:
        fields = ["source", "candidate_id", "recipe", "suggestion_id", "field", "diagnosis",
                  "detail", "source_evidence", "suggested_completion", "confidence",
                  "uncertainty", "review_status", "approved_value", "suggested_action"]
        path.parent.mkdir(parents=True, exist_ok=True)
        entries = dict(self.candidates)
        for identity, registrations in self.registrations.items():
            if registrations:
                registration = registrations[-1]
                entries.setdefault(identity, {
                    "source": registration.get("source", ""),
                    "candidate_id": identity,
                    "candidate": registration["candidate"],
                })
        with path.open("w", newline="", encoding="utf-8-sig") as fh:
            writer = csv.DictWriter(fh, fieldnames=fields)
            writer.writeheader()
            for identity, event in entries.items():
                candidate = RecipeCandidate.model_validate(event["candidate"])
                decisions = {d.suggestion_id: d for d in self.decisions_for(identity)}
                registration = self.registrations.get(identity, [])
                registration = registration[-1] if registration else None
                if registration:
                    revisions = [item for item in self.revisions.get(identity, [])
                                 if item.get("source_hash") == registration.get("source_hash")
                                 and item.get("extraction_id") == registration.get("extraction_id")]
                    current_revision = max((int(item.get("revision", 0)) for item in revisions), default=0)
                    dispositions = {
                        "accepted": "approved", "human_completion": "approved",
                        "edited": "edited", "source_corrected": "edited",
                        "rejected": "rejected", "not_recipe_content": "rejected",
                        "deferred": "pending",
                    }
                    for resolution in self.resolutions.get(identity, []):
                        if (resolution.get("source_hash") == registration.get("source_hash")
                                and resolution.get("extraction_id") == registration.get("extraction_id")
                                and int(resolution.get("revision", -1)) == current_revision):
                            status = dispositions.get(resolution.get("disposition"))
                            if status:
                                decisions[resolution["issue_id"]] = ReviewDecision(
                                    resolution["issue_id"], status, resolution.get("final_value"),
                                    resolution.get("timestamp", ""),
                                )
                suggestions = {}
                for suggestion in candidate.suggestions:
                    suggestions.setdefault(suggestion.field_path, []).append(suggestion)
                written_fields = set()
                written_suggestions = set()
                for finding_index, finding in enumerate(candidate.missing_information):
                    written_fields.add(finding.field_path)
                    field_suggestions = suggestions.get(finding.field_path, [])
                    if not field_suggestions:
                        issue_id = f"finding:{finding_index}:{finding.field_path}"
                        decision = decisions.get(issue_id)
                        field_suggestions = [None]
                    else:
                        decision = None
                    for suggestion in field_suggestions:
                        if suggestion and suggestion.suggestion_id in written_suggestions:
                            continue
                        if suggestion:
                            written_suggestions.add(suggestion.suggestion_id)
                        decision = (decisions.get(suggestion.suggestion_id)
                                    if suggestion else decision)
                        writer.writerow({
                            "source": event["source"], "candidate_id": identity,
                            "recipe": candidate.name,
                            "suggestion_id": suggestion.suggestion_id if suggestion else "",
                            "field": finding.field_path,
                            "diagnosis": finding.kind, "detail": finding.detail,
                            "source_evidence": finding.source_evidence,
                            "suggested_completion": suggestion.proposed_value if suggestion else "",
                            "confidence": suggestion.confidence if suggestion else "",
                            "uncertainty": suggestion.uncertainty if suggestion else "",
                            "review_status": decision.status if decision else "pending",
                            "approved_value": (decision.final_value if decision and decision.status in {"approved", "edited"}
                                               and decision.final_value is not None else
                                               suggestion.proposed_value if suggestion and decision
                                               and decision.status == "approved" else ""),
                            "suggested_action": finding.suggested_action,
                        })
                for suggestion in candidate.suggestions:
                    if suggestion.suggestion_id in written_suggestions:
                        continue
                    decision = decisions.get(suggestion.suggestion_id)
                    writer.writerow({
                        "source": event["source"], "candidate_id": identity,
                        "recipe": candidate.name, "suggestion_id": suggestion.suggestion_id,
                        "field": suggestion.field_path,
                        "diagnosis": "missing_from_source", "detail": "Unverified proposal requires review.",
                        "source_evidence": "", "suggested_completion": suggestion.proposed_value,
                        "confidence": suggestion.confidence, "uncertainty": suggestion.uncertainty,
                        "review_status": decision.status if decision else suggestion.review_status,
                        "approved_value": decision.final_value if decision and decision.status in {"approved", "edited"} else "",
                        "suggested_action": "Approve, edit, reject, or defer this proposal.",
                    })
                if candidate.requires_review and not written_fields and not candidate.suggestions:
                    decision = self.candidate_decision(identity)
                    writer.writerow({
                        "source": event["source"], "candidate_id": identity,
                        "recipe": candidate.name, "suggestion_id": "",
                        "field": "recipe", "diagnosis": "manual_review",
                        "detail": "OCR-derived recipe requires explicit human approval or edit.",
                        "source_evidence": "", "suggested_completion": "",
                        "confidence": candidate.confidence,
                        "uncertainty": "; ".join(candidate.uncertainties),
                        "review_status": decision.status if decision else "pending",
                        "approved_value": decision.final_value if decision and decision.status == "edited" else "",
                        "suggested_action": "Approve or edit the recipe before importing.",
                    })


def apply_review_decision(candidate: RecipeCandidate,
                          decisions: list[ReviewDecision]) -> RecipeCandidate:
    """Apply only approved/edited values, retaining the original proposal."""
    updated = candidate.model_copy(deep=True)
    by_id = {item.suggestion_id: item for item in decisions}
    for suggestion in updated.suggestions:
        decision = by_id.get(suggestion.suggestion_id)
        if decision is None:
            continue
        suggestion.review_status = decision.status
        if decision.status not in {"approved", "edited"}:
            continue
        value = decision.final_value or suggestion.proposed_value
        _set_field(updated, suggestion.field_path, value)
    return updated


def apply_candidate_decision(candidate: RecipeCandidate,
                             decision: ReviewDecision | None) -> RecipeCandidate:
    """Apply an explicit whole-candidate approval/edit without changing provenance."""
    if decision is None:
        return candidate
    updated = candidate.model_copy(deep=True)
    if decision.status == "rejected":
        return updated
    if decision.status == "edited":
        try:
            values = json.loads(decision.final_value or "")
        except json.JSONDecodeError as exc:
            raise ValueError("Edited candidate review value must be valid JSON") from exc
        if not isinstance(values, dict):
            raise ValueError("Edited candidate review value must be a JSON object")
        if "name" in values:
            updated.name = str(values["name"])
        for field in ("ingredients", "instructions"):
            if field in values:
                value = values[field]
                if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                    raise ValueError(f"Edited candidate {field} must be a list of strings")
                setattr(updated, field, value)
        for field in ("yield_text", "description"):
            if field in values:
                setattr(updated, field, None if values[field] is None else str(values[field]))
    if decision.status in {"approved", "edited"}:
        updated.requires_review = False
    return updated


def _set_field(candidate: RecipeCandidate, field_path: str, value: str) -> None:
    if field_path == "name":
        candidate.name = value
        return
    if field_path in {"yield_text", "description"}:
        setattr(candidate, field_path, value)
        return
    if field_path in {"ingredients", "instructions"}:
        setattr(candidate, field_path, _split_list_value(value))
        return
    for prefix, target in (("ingredients", candidate.ingredients), ("instructions", candidate.instructions)):
        if field_path.startswith(prefix + "[") and field_path.endswith("]"):
            try:
                index = int(field_path[len(prefix) + 1:-1])
                if index < 0:
                    raise ValueError
                while len(target) <= index:
                    target.append("")
                target[index] = value
                return
            except ValueError as exc:
                raise ValueError(f"Invalid review field path: {field_path}") from exc
    raise ValueError(f"Unsupported review field path: {field_path}")


def _split_list_value(value: str) -> list[str]:
    text = value.strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, list):
        if not all(isinstance(item, str) for item in parsed):
            raise ValueError("Review list value must contain only strings")
        return [item.strip() for item in parsed if item.strip()]
    entries = []
    for line in text.splitlines():
        line = re.sub(r"^\s*(?:\d+\s*[.)]|[-*•])\s*", "", line).strip()
        if line:
            entries.append(line)
    return entries
