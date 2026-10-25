"""Append-only local review queue for extraction diagnostics and proposals."""
from __future__ import annotations

import csv
import ctypes
import json
import os
import re
import hashlib
import socket
import uuid
from collections import Counter
import time
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from recipe_quality import RecipeCandidate, Suggestion, is_complete
from recipe_validation import validate_recipe
from ui_diagnostics import count as ui_count


class ReviewConflict(RuntimeError):
    """A stale revision or reused operation ID cannot be safely committed."""


MANUAL_APPROVAL_ACKNOWLEDGMENT = (
    "I have reviewed the recipe as currently written. I understand that the original source is unavailable "
    "and that this approval does not verify the transcription against the original document."
)


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
    approval_method: str = ""
    approval_rationale: str = ""
    no_instructions_reason: str = ""


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


def _windows_process_is_alive(pid: int, kernel32=None) -> bool:
    """Query process state without sending a signal or terminating the process."""
    from ctypes import wintypes

    api = kernel32 or ctypes.WinDLL("kernel32", use_last_error=True)
    api.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    api.OpenProcess.restype = wintypes.HANDLE
    api.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    api.GetExitCodeProcess.restype = wintypes.BOOL
    api.CloseHandle.argtypes = (wintypes.HANDLE,)
    api.CloseHandle.restype = wintypes.BOOL

    # QUERY_LIMITED_INFORMATION | SYNCHRONIZE
    handle = api.OpenProcess(0x1000 | 0x00100000, False, pid)
    if not handle:
        # Only ERROR_INVALID_PARAMETER definitively means that PID does not
        # identify a process. Treat access denied and other unknown failures as
        # live to avoid replaying an uncertain provider request.
        return ctypes.get_last_error() != 87
    try:
        exit_code = wintypes.DWORD()
        if not api.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return True
        return exit_code.value == 259  # STILL_ACTIVE
    finally:
        api.CloseHandle(handle)


def _process_is_alive(pid: int) -> bool:
    if os.name == "nt":
        return _windows_process_is_alive(pid)
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except (ProcessLookupError, OSError):
        return False


def _canonical(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _candidate_payload(candidate: RecipeCandidate) -> dict:
    value = candidate.model_dump(mode="json")
    if not value.get("notes"):
        value.pop("notes", None)
    return value


def _range_target_bindings(candidate: RecipeCandidate) -> list[dict]:
    bindings = []
    field_targets = {}
    for field in ("ingredients", "instructions"):
        for position, value in enumerate(getattr(candidate, field)):
            bindings.append({"target_id": str(uuid.uuid4()), "target_kind": "row",
                             "field": field, "original_value": value,
                             "original_position": position})

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
                      "field": field_path, "original_value": getattr(candidate, field_path)}
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
                                   if source_target_kind == "row" else suggestion.field_path),
                         "original_value": suggestion.proposed_value,
                         "original_issue": suggestion.model_dump(mode="json")})
    for index, finding in enumerate(candidate.missing_information):
        source_target, source_target_kind = target_for_path(finding.field_path)
        bindings.append({"issue_id": f"finding:{index}:{finding.field_path}",
                         "target_id": str(uuid.uuid4()), "target_kind": "finding",
                         "field_path_at_extraction": finding.field_path,
                         "severity": finding.severity,
                         "source_target_id": source_target,
                         "source_target_kind": source_target_kind,
                         "field": (finding.field_path.split("[", 1)[0]
                                   if source_target_kind == "row" else finding.field_path),
                         "original_value": finding.detail,
                         "original_issue": finding.model_dump(mode="json")})
    return bindings


def _recipe_digest(recipe: dict) -> str:
    fields = {key: recipe.get(key) for key in (
        "name", "ingredients", "instructions", "yield_text", "description",
        "prep_time", "perform_time", "total_time", "recipe_category", "tags", "notes",
    ) if key in recipe}
    if not fields.get("notes"):
        fields.pop("notes", None)
    return hashlib.sha256(b"recipe-content-v1\0" + _canonical(fields).encode("utf-8")).hexdigest()


_IDENTITY_FIELDS = (
    "name", "ingredients", "instructions", "yield_text", "description", "notes",
    "prep_time", "perform_time", "total_time", "recipe_category", "tags",
)


def authoritative_recipe_payload(recipe: dict) -> dict:
    """Return conservative, versioned source-content fields for logical identity."""
    payload = {}
    for field in _IDENTITY_FIELDS:
        value = recipe.get(field)
        if field in {"yield_text", "description"}:
            value = value or None
        elif field in {"ingredients", "instructions", "notes", "tags"}:
            value = value or []
        elif field == "name":
            value = value or ""
        payload[field] = value
    return payload


def logical_recipe_identity(recipe: dict) -> str:
    """Hash authoritative recipe content; extraction metadata is intentionally excluded."""
    return hashlib.sha256(
        b"logical-recipe-v1\0" + _canonical(authoritative_recipe_payload(recipe)).encode("utf-8")
    ).hexdigest()


def authoritative_recipe_differences(left: dict, right: dict) -> dict[str, tuple[object, object]]:
    a, b = authoritative_recipe_payload(left), authoritative_recipe_payload(right)
    return {key: (a[key], b[key]) for key in _IDENTITY_FIELDS if a[key] != b[key]}


def _resolution_digest(resolutions: list[dict]) -> str:
    values = [{key: value for key, value in item.items()
               if key not in {"event_id", "timestamp", "actor"}}
              for item in resolutions]
    values.sort(key=lambda item: (item.get("issue_id", ""), item.get("target_id", ""),
                                  item.get("operation_id", "")))
    return hashlib.sha256(_canonical(values).encode("utf-8")).hexdigest()


def extraction_identity(source_hash: str, extraction: dict) -> str:
    normalized = json.loads(json.dumps(extraction, ensure_ascii=False))
    for candidate in normalized.get("recipes", []) if isinstance(normalized, dict) else []:
        if isinstance(candidate, dict) and not candidate.get("notes"):
            candidate.pop("notes", None)
    return hashlib.sha256((source_hash + _canonical(normalized)).encode("utf-8")).hexdigest()


_RANGE_PROVENANCE_FIELDS = (
    "schema_version", "kind", "batch_id", "parent_document_id", "parent_sha256",
    "range_id", "first_page", "last_page", "page_count", "input_sha256",
    "cache_identity", "mode", "config_fingerprint",
)


def range_extraction_identity(provenance: dict, candidates: list[RecipeCandidate]) -> str:
    """Identify one range extraction without relying on result-array order."""
    identity = {key: provenance.get(key) for key in _RANGE_PROVENANCE_FIELDS}
    rows = sorted((_candidate_payload(candidate) for candidate in candidates), key=_canonical)
    return hashlib.sha256(
        b"recipe-importer:range-extraction:v1\0" + _canonical({"identity": identity, "recipes": rows}).encode()
    ).hexdigest()


def range_occurrence_id(provenance: dict, extraction_id: str, fingerprint: str,
                        identical_occurrence: int) -> str:
    """Return a stable ID for one same-content occurrence within one range extraction."""
    identity = {key: provenance.get(key) for key in (
        "schema_version", "kind", "batch_id", "parent_document_id", "parent_sha256",
        "range_id", "first_page", "last_page", "page_count", "input_sha256",
    )}
    identity.update({"extraction_id": extraction_id, "fingerprint": fingerprint,
                     "identical_occurrence": identical_occurrence})
    return hashlib.sha256(
        b"recipe-importer:range-candidate:v1\0" + _canonical(identity).encode()
    ).hexdigest()


def range_candidate_id(provenance: dict, extraction_id: str, fingerprint: str,
                       identical_occurrence: int) -> str:
    return f"range-v1:{range_occurrence_id(provenance, extraction_id, fingerprint, identical_occurrence)}"


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
        self._exact_registration_index: dict[tuple[str, str, str], list[dict]] = {}
        self._source_registration_index: dict[str, list[dict]] = {}
        self._source_event_index: dict[str, list[dict]] = {}
        self._source_path_event_index: dict[str, list[dict]] = {}
        self._candidate_history_index: dict[str, Counter] = {}
        self._identity_link_group_index: dict[tuple[str, str, str], str] = {}
        self._protected_source_cache: set[str] | None = None
        with _state_lock(path):
            self._reload()

    def _reload(self) -> None:
        self.candidates.clear()
        self.decisions.clear()
        self.candidate_decisions.clear()
        self.registrations = {}
        self.revisions = {}
        self.approvals = {}
        self.no_instructions_decisions = {}
        self.source_relinks = {}
        self.resolutions = {}
        self.events = []
        self._exact_registration_index = {}
        self._source_registration_index = {}
        self._source_event_index = {}
        self._source_path_event_index = {}
        self._candidate_history_index = {}
        self._identity_link_group_index = {}
        self._protected_source_cache = None
        self._suggestion_requests_by_candidate: dict[str, list[dict]] = {}
        self._suggestion_requests_by_id: dict[str, dict] = {}
        self._suggestion_request_status_by_id: dict[str, dict] = {}
        self._dynamic_suggestions_by_candidate: dict[str, list[dict]] = {}
        self._dynamic_suggestions_by_id: dict[str, dict] = {}
        self._dynamic_decisions_by_candidate: dict[str, dict[str, dict]] = {}
        if self.path.exists():
            ui_count("ledger_reads")
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                event = json.loads(line)
                self.events.append(event)
                self._index_suggestion_event(event)
                identity = event.get("candidate_id", "")
                kind = event.get("event")
                if kind == "candidate_registration":
                    self.registrations.setdefault(identity, []).append(event)
                    self._index_registration(event)
                    self._source_registration_index.setdefault(str(event.get("source_hash", "")), []).append(event)
                elif kind == "review_revision":
                    self.revisions.setdefault(identity, []).append(event)
                elif kind == "review_approval":
                    self.approvals.setdefault(identity, []).append(event)
                elif kind == "review_no_instructions_decision":
                    self.no_instructions_decisions.setdefault(identity, []).append(event)
                elif kind == "review_source_relink":
                    self.source_relinks.setdefault(identity, []).append(event)
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
                history_kinds = {"review_revision": "revisions", "review_approval": "approvals",
                                 "decision": "suggestion_decisions", "candidate_decision": "review_decisions",
                                 "dynamic_suggestion": "generated_proposals",
                                 "dynamic_suggestion_decision": "proposal_decisions"}
                if kind in history_kinds:
                    self._candidate_history_index.setdefault(identity, Counter())[history_kinds[kind]] += 1
        self._index_valid_identity_links()
        path_sources: dict[str, set[str]] = {}
        for source, items in self._source_registration_index.items():
            for item in items:
                path = str(item.get("source", "")).casefold()
                if path:
                    path_sources.setdefault(path, set()).add(source)
        for event in self.events:
            if event.get("event") != "candidate":
                continue
            identity = str(event.get("candidate_id", ""))
            if event.get("source"):
                self._source_path_event_index.setdefault(str(event["source"]).casefold(), []).append(event)
            prefix, separator, _ = identity.partition(":")
            if separator and len(prefix) == 64:
                self._source_event_index.setdefault(prefix, []).append(event)
            elif event.get("source"):
                for source in path_sources.get(str(event["source"]).casefold(), set()):
                    self._source_event_index.setdefault(source, []).append(event)

    def _append(self, event: dict, *, locked: bool = False) -> None:
        self._append_many([event], locked=locked)

    def _append_many(self, events: list[dict], *, locked: bool = False) -> None:
        if not events:
            return
        if not locked:
            with _state_lock(self.path):
                self._reload()
                self._append_many(events, locked=True)
                self._reload()
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write("".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events))
            fh.flush()
            os.fsync(fh.fileno())
        self.events.extend(events)
        for event in events:
            self._index_suggestion_event(event)

    def _index_suggestion_event(self, event: dict) -> None:
        """Keep suggestion lookups proportional to one candidate, not the ledger."""
        kind = event.get("event")
        candidate_id = str(event.get("candidate_id", ""))
        request_id = str(event.get("request_id", ""))
        if kind == "suggestion_request":
            self._suggestion_requests_by_candidate.setdefault(candidate_id, []).append(event)
            self._suggestion_requests_by_id[request_id] = event
            self._suggestion_request_status_by_id[request_id] = {
                key: value for key, value in event.items()
                if key not in {"event", "event_id", "timestamp", "actor"}
            }
        elif kind == "suggestion_request_status" and request_id:
            current = self._suggestion_request_status_by_id.get(request_id)
            if current is not None:
                current.update({key: value for key, value in event.items()
                                if key not in {"event", "event_id", "timestamp", "actor"}})
        elif kind == "dynamic_suggestion":
            self._dynamic_suggestions_by_candidate.setdefault(candidate_id, []).append(event)
            self._dynamic_suggestions_by_id[str(event.get("proposal_id", ""))] = event
        elif kind == "dynamic_suggestion_decision":
            decisions = self._dynamic_decisions_by_candidate.setdefault(candidate_id, {})
            decisions[str(event.get("proposal_id", ""))] = event

    def _readback(self, event: dict) -> dict:
        readback = next((item for item in self.events if item.get("event_id") == event["event_id"]), None)
        if readback is None or _canonical(readback) != _canonical(event):
            raise ReviewConflict("durable review event readback did not match the appended event")
        return readback

    def _protected_import_outcome(self, registration: dict, *, state_records: list[dict] | None = None,
                                  protected_ids: set[str] | None = None) -> dict | None:
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
        if state_records is None:
            try:
                if self.state_path.exists():
                    ui_count("ledger_reads")
                records = ([json.loads(line) for line in state_path.read_text(encoding="utf-8").splitlines()
                            if line.strip()] if state_path.exists() else [])
                if any(not isinstance(record, dict) for record in records):
                    raise ValueError("invalid import state record")
            except (OSError, json.JSONDecodeError, ValueError) as exc:
                raise ReviewConflict("import state is unreadable; approval is protected pending reconciliation") from exc
        else:
            records = state_records
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
        if protected_ids is None:
            try:
                protected = protected_candidate_ids(self.path.parent, state_path=state_path)
            except ImportProtectionError as exc:
                raise ReviewConflict(str(exc)) from exc
        else:
            protected = protected_ids
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

    def _identity_link_event(self, candidate_id: str, previous: dict, source_hash: str,
                             extraction_id: str, identity_digest: str,
                             identity_group_id: str, decision: str) -> dict:
        operation_id = f"identity-link:{decision}:{source_hash}:{extraction_id}:{candidate_id}"
        event = self._base_event("candidate_identity_link", candidate_id, source_hash,
                                 extraction_id, operation_id)
        event.update({"previous_candidate_id": str(previous["candidate_id"]),
                      "previous_extraction_id": str(previous.get("extraction_id", "")),
                      "identity_group_id": identity_group_id,
                      "logical_identity_digest": identity_digest,
                      "comparison_version": 1, "decision": decision})
        return event

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

    def register_range_candidates(self, source: str, source_hash: str,
                                  provenance: dict,
                                  candidates: list[RecipeCandidate]) -> list[dict]:
        """Append candidates whose identity is isolated to a verified cookbook range."""
        if (not isinstance(provenance, dict)
                or any(key not in provenance for key in _RANGE_PROVENANCE_FIELDS)
                or provenance.get("schema_version") != 1
                or provenance.get("kind") != "cookbook_range_v1"
                or provenance.get("parent_sha256") != source_hash
                or provenance.get("parent_document_id") != source_hash
                or not re.fullmatch(r"[0-9a-f]{64}", str(source_hash))
                or not re.fullmatch(r"[0-9a-f]{64}", str(provenance.get("range_id", "")))
                or not re.fullmatch(r"[0-9a-f]{64}", str(provenance.get("input_sha256", "")))
                or not re.fullmatch(r"[0-9a-f]{64}", str(provenance.get("cache_identity", "")))
                or not re.fullmatch(r"[0-9a-f]{64}", str(provenance.get("config_fingerprint", "")))
                or not re.fullmatch(r"[0-9a-f]{64}", str(provenance.get("cache_record_sha256", "")))
                or type(provenance.get("first_page")) is not int
                or type(provenance.get("last_page")) is not int
                or type(provenance.get("page_count")) is not int
                or provenance["first_page"] < 1
                or provenance["last_page"] < provenance["first_page"]
                or provenance["page_count"] != provenance["last_page"] - provenance["first_page"] + 1
                or not provenance.get("batch_id") or not provenance.get("mode")
                or not provenance.get("parent_filename") or not provenance.get("extractor_version")
                or not provenance.get("cache_path") or not provenance.get("source_path") or not source):
            raise ValueError("Cookbook range provenance is invalid or incomplete.")
        candidates = [RecipeCandidate.model_validate(candidate) for candidate in candidates]
        extraction_id = range_extraction_identity(provenance, candidates)
        fingerprints = [hashlib.sha256(_canonical(_candidate_payload(item)).encode()).hexdigest()
                        for item in candidates]
        occurrence_counts: Counter[str] = Counter()
        identities = []
        for fingerprint in fingerprints:
            occurrence = occurrence_counts[fingerprint]
            occurrence_counts[fingerprint] += 1
            identities.append((fingerprint, occurrence,
                               range_candidate_id(provenance, extraction_id, fingerprint, occurrence)))

        with _state_lock(self.path):
            self._reload()
            output = []
            pending = []
            for candidate, (fingerprint, occurrence, candidate_id) in zip(candidates, identities):
                existing = [event for event in self.registrations.get(candidate_id, [])
                            if event.get("event") == "candidate_registration"]
                if existing:
                    previous_provenance = existing[0].get("range_provenance", {}) if len(existing) == 1 else {}
                    same_provenance = all(
                        previous_provenance.get(key) == provenance.get(key)
                        for key in (*_RANGE_PROVENANCE_FIELDS, "parent_filename", "input_path",
                                    "source_path", "extraction_method", "extractor_version")
                    )
                    if (len(existing) != 1
                            or existing[0].get("source_hash") != source_hash
                            or existing[0].get("extraction_id") != extraction_id
                            or existing[0].get("fingerprint") != fingerprint
                            or not same_provenance
                            or existing[0].get("source") != str(source)
                            or existing[0].get("candidate") != _candidate_payload(candidate)
                            or existing[0].get("range_occurrence") != occurrence):
                        raise ReviewConflict("Range candidate identity conflicts with existing review history.")
                    output.append(existing[0])
                    continue
                bindings = _range_target_bindings(candidate)
                event = self._base_event(
                    "candidate_registration", candidate_id, source_hash, extraction_id,
                    f"register-range-v1:{candidate_id}",
                )
                event.update({
                    "source": str(source), "fingerprint": fingerprint,
                    "identity_status": "registered", "identity_version": 2,
                    "logical_identity_digest": logical_recipe_identity(_candidate_payload(candidate)),
                    "identity_group_id": candidate_id, "legacy_id": "",
                    "title_legacy_id": "", "legacy_aliases": [],
                    "candidate": _candidate_payload(candidate),
                    "target_bindings": bindings, "range_provenance": dict(provenance),
                    "range_occurrence": occurrence,
                    "range_occurrence_id": candidate_id.partition(":")[2],
                })
                pending.append(event)
                output.append(event)
            self._append_many(pending, locked=True)
            self._reload()
            return [next((event for event in self.registrations.get(item["candidate_id"], [])
                          if event.get("source_hash") == source_hash
                          and event.get("extraction_id") == extraction_id), item)
                    for item in output]

    def register_candidate_batches(
        self, batches: list[tuple[str, str, str, list[RecipeCandidate]]],
    ) -> list[list[dict]]:
        """Register multiple source batches under one fresh lock and durable append."""
        if not batches:
            return []
        exact = [self._exact_registration_batch(source_hash, extraction_id, candidates)
                 for _, source_hash, extraction_id, candidates in batches]
        if all(items is not None for items in exact):
            return exact
        with _state_lock(self.path):
            self._reload()
            pending_events: list[dict] = []
            output = [self.register_candidates(
                source, source_hash, extraction_id, candidates,
                _lock_held=True, _pending_events=pending_events,
            ) for source, source_hash, extraction_id, candidates in batches]
            self._append_many(pending_events, locked=True)
            for event in pending_events:
                if event.get("event") == "candidate_registration":
                    self.registrations.setdefault(event["candidate_id"], []).append(event)
                    self._index_registration(event)
            return output

    def register_candidates(self, source: str, source_hash: str, extraction_id: str,
                            candidates: list[RecipeCandidate], *, _lock_held: bool = False,
                            _pending_events: list[dict] | None = None) -> list[dict]:
        exact = (None if _lock_held else
                 self._exact_registration_batch(source_hash, extraction_id, candidates))
        if exact is not None:
            # Catalog reads commonly revisit every source in an already
            # registered library. Registrations are immutable, so reuse this
            # loaded snapshot; any missing identity still takes the locked,
            # fresh-read mutation path below.
            return exact
        with (nullcontext() if _lock_held else _state_lock(self.path)):
            if not _lock_held:
                self._reload()
            source_registrations = list(self._source_registration_index.get(source_hash, []))
            indexed_source_events = [*self._source_event_index.get(source_hash, []),
                                     *self._source_path_event_index.get(str(source).casefold(), [])]
            source_events = list({str(event.get("event_id") or id(event)): event
                                  for event in indexed_source_events}.values())
            source_registrations.extend(event for event in (_pending_events or [])
                                        if event.get("event") == "candidate_registration"
                                        and event.get("source_hash") == source_hash)
            source_events.extend(event for event in (_pending_events or [])
                                 if event.get("event") == "candidate"
                                 and (str(event.get("candidate_id", "")).startswith(source_hash + ":")
                                      or str(event.get("source", "")).casefold()
                                      == str(source).casefold()))
            prior_source_registration = bool(source_registrations)
            source_import_protected = self._source_has_protected_import_history(source_hash)
            fingerprints = [hashlib.sha256(_canonical(_candidate_payload(item)).encode()).hexdigest()
                            for item in candidates]
            current_payloads = {_canonical(_candidate_payload(item)) for item in candidates}
            prior_source_legacy_candidate = any(
                event.get("event") == "candidate"
                and (str(event.get("candidate_id", "")).startswith(source_hash + ":")
                     or str(event.get("source", "")).casefold() == str(source).casefold())
                and _canonical(event.get("candidate", {})) not in current_payloads
                for event in source_events
            )
            has_prior_source = (prior_source_registration or prior_source_legacy_candidate
                                or source_import_protected)
            repeated = {value for value, count in Counter(fingerprints).items() if count > 1}
            identity_digests = [logical_recipe_identity(_candidate_payload(item)) for item in candidates]
            repeated_identity_digests = {
                value for value, count in Counter(identity_digests).items() if count > 1
            }
            titles = [item.name.casefold().strip() for item in candidates]
            title_counts = Counter(titles)
            exact_index: dict[str, list[dict]] = {}
            prior_index: dict[str, list[dict]] = {}
            prior_identity_index: dict[str, list[dict]] = {}
            all_identity_index: dict[str, list[dict]] = {}
            for event in source_registrations:
                if event.get("extraction_id") == extraction_id:
                    exact_index.setdefault(event["fingerprint"], []).append(event)
                payload = event.get("candidate", {})
                digest = event.get("logical_identity_digest") or logical_recipe_identity(payload)
                all_identity_index.setdefault(digest, []).append(event)
                if event.get("identity_status") == "registered":
                    prior_index.setdefault(event["fingerprint"], []).append(event)
                    prior_identity_index.setdefault(digest, []).append(event)
            for event in (_pending_events or []):
                if event.get("source_hash") != source_hash:
                    continue
                if event.get("extraction_id") == extraction_id:
                    exact_index.setdefault(event["fingerprint"], []).append(event)
                payload = event.get("candidate", {})
                digest = event.get("logical_identity_digest") or logical_recipe_identity(payload)
                all_identity_index.setdefault(digest, []).append(event)
                if event.get("identity_status") == "registered":
                    prior_index.setdefault(event["fingerprint"], []).append(event)
                    prior_identity_index.setdefault(digest, []).append(event)
            legacy_index: dict[str, list[str]] = {}
            for event in source_events:
                if event.get("event") == "candidate":
                    identity = str(event.get("candidate_id", ""))
                    legacy_index.setdefault(_canonical(event.get("candidate", {})), []).append(identity)
            occurrences: dict[str, int] = {}
            output = []
            pending_events = []
            for candidate, fingerprint, identity_digest in zip(candidates, fingerprints, identity_digests):
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
                elif (fingerprint not in repeated
                      and identity_digest not in repeated_identity_digests
                      and prior_identity_index.get(identity_digest)
                      and not source_import_protected
                      and not prior_source_legacy_candidate):
                    groups: dict[str, list[dict]] = {}
                    unlinked_conflict = False
                    for match in all_identity_index.get(identity_digest, []):
                        group_id = (str(match.get("identity_group_id") or "")
                                    or self._identity_link_group_index.get((str(match["candidate_id"]),
                                        source_hash, str(match.get("extraction_id", "")))))
                        if not group_id and match.get("identity_status") == "registered":
                            group_id = str(match["candidate_id"])
                        if not group_id:
                            unlinked_conflict = True
                        else:
                            groups.setdefault(group_id, []).append(match)
                    if len(groups) == 1 and not unlinked_conflict:
                        identity_group_id, group = next(iter(groups.items()))
                        previous = max(group, key=lambda item: str(item.get("timestamp", "")))
                        candidate_id, identity_status = str(uuid.uuid4()), "registered"
                        pending_events.append(self._identity_link_event(
                            candidate_id, previous, source_hash, extraction_id, identity_digest,
                            identity_group_id, "automatic_unique_content_match",
                        ))
                    else:
                        candidate_id, identity_status = str(uuid.uuid4()), "needs_reconciliation"
                elif fingerprint not in repeated:
                    legacy_matches = legacy_index.get(_canonical(_candidate_payload(candidate)), [])
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
                identity_group_id = str(candidate_id) if identity_status == "registered" else ""
                link_event = next((item for item in pending_events
                                   if item.get("event") == "candidate_identity_link"
                                   and item.get("candidate_id") == candidate_id), None)
                if link_event:
                    identity_group_id = str(link_event["identity_group_id"])
                event.update({"source": source, "fingerprint": fingerprint,
                              "identity_status": identity_status,
                              "identity_version": 1,
                              "logical_identity_digest": identity_digest,
                              "identity_group_id": identity_group_id,
                              "legacy_id": f"{source_hash}:{fingerprint[:24]}",
                              "title_legacy_id": title_legacy_id,
                              "legacy_aliases": [f"{source_hash}:{fingerprint[:24]}",
                                                 *([title_legacy_id] if title_legacy_id else [])],
                              "candidate": _candidate_payload(candidate),
                              "target_bindings": bindings})
                # Registration retries are keyed by immutable extraction/fingerprint.
                existing = next((e for e in [*self.events, *(_pending_events or [])]
                                 if e.get("event") == event["event"]
                                 and e.get("operation_id") == event["operation_id"]), None)
                if existing:
                    output.append(existing)
                    continue
                pending_events.append(event)
                exact_index.setdefault(fingerprint, []).append(event)
                if identity_status == "registered":
                    prior_index.setdefault(fingerprint, []).append(event)
                output.append(event)
            if _pending_events is None:
                self._append_many(pending_events, locked=True)
                for event in pending_events:
                    if event.get("event") == "candidate_registration":
                        self.registrations.setdefault(event["candidate_id"], []).append(event)
                        self._index_registration(event)
            else:
                _pending_events.extend(pending_events)
            return output

    def _index_valid_identity_links(self) -> None:
        accepted = {"confirmed_unique_content_match", "confirmed_user_selected_match",
                    "automatic_unique_content_match"}
        grouped: dict[tuple[str, str, str], list[dict]] = {}
        event_ids = Counter(str(item.get("event_id", "")) for item in self.events)
        operation_ids = Counter(str(item.get("operation_id", "")) for item in self.events)
        for event in self.events:
            if event.get("event") not in {"candidate_identity_link", "candidate_identity_review"}:
                continue
            if event.get("event") != "candidate_identity_link" or event.get("decision") not in accepted:
                continue
            key = (str(event.get("candidate_id", "")), str(event.get("source_hash", "")),
                   str(event.get("extraction_id", "")))
            grouped.setdefault(key, []).append(event)

        for key, events in grouped.items():
            candidate_id, source_hash, extraction_id = key
            if (len(events) != 1 or not all(key)
                    or not events[0].get("event_id") or event_ids[events[0]["event_id"]] != 1
                    or not events[0].get("operation_id") or operation_ids[events[0]["operation_id"]] != 1):
                continue
            event = events[0]
            if (event.get("schema_version") != 1 or event.get("comparison_version") != 1
                    or not event.get("identity_group_id") or not event.get("logical_identity_digest")):
                continue
            current = [item for item in self.registrations.get(candidate_id, [])
                       if item.get("source_hash") == source_hash
                       and item.get("extraction_id") == extraction_id]
            if len(current) != 1 or current[0].get("identity_status") != "needs_reconciliation":
                continue
            held = current[0]
            digest = logical_recipe_identity(held.get("candidate", {}))
            if (held.get("logical_identity_digest")
                    and held.get("logical_identity_digest") != digest):
                continue
            if digest != event.get("logical_identity_digest"):
                continue
            if (event.get("decision") == "confirmed_user_selected_match"
                    and not str(event.get("evidence", "")).strip()):
                continue
            previous_id = str(event.get("previous_candidate_id", ""))
            previous_extraction_id = str(event.get("previous_extraction_id", ""))
            previous = [item for item in self.registrations.get(previous_id, [])
                        if item.get("source_hash") == source_hash
                        and (not previous_extraction_id
                             or item.get("extraction_id") == previous_extraction_id)
                        and (item.get("logical_identity_digest")
                             or logical_recipe_identity(item.get("candidate", {}))) == digest]
            if len(previous) != 1 or previous_id == candidate_id:
                continue
            prior = previous[0]
            prior_digest = logical_recipe_identity(prior.get("candidate", {}))
            if (prior.get("logical_identity_digest")
                    and prior.get("logical_identity_digest") != prior_digest):
                continue
            if prior_digest != digest:
                continue
            prior_key = (previous_id, source_hash, str(prior.get("extraction_id", "")))
            if prior.get("identity_status") == "registered":
                prior_group = str(prior.get("identity_group_id") or previous_id)
            elif (prior.get("identity_status") == "needs_reconciliation"
                  and self._identity_link_group_index.get(prior_key)):
                prior_group = self._identity_link_group_index[prior_key]
            else:
                continue
            if str(event["identity_group_id"]) != prior_group:
                continue
            self._identity_link_group_index[key] = prior_group

    def _effective_identity_authority(
        self, candidate_id: str, source_hash: str, extraction_id: str, *,
        registration: dict | None = None, check_import_protection: bool = False,
    ) -> tuple[dict | None, str, bool]:
        """Resolve one exact registration and its durable link; keep import protection separate."""
        matches = [item for item in self.registrations.get(candidate_id, [])
                   if item.get("source_hash") == source_hash
                   and item.get("extraction_id") == extraction_id]
        if registration is not None:
            matches = [item for item in matches if item is registration]
        if len(matches) != 1:
            return None, "needs_reconciliation", False
        exact = matches[0]
        status = str(exact.get("identity_status", "needs_reconciliation"))
        key = (candidate_id, source_hash, extraction_id)
        if status == "needs_reconciliation" and self._identity_link_group_index.get(key):
            status = "registered"
        protected = bool(check_import_protection and status == "registered"
                         and self._protected_import_outcome(exact))
        return exact, status, protected

    def identity_link_status(self, candidate_id: str, source_hash: str, extraction_id: str) -> str:
        return self._effective_identity_authority(candidate_id, source_hash, extraction_id)[1]

    def identity_reconciliation_candidates(self, source_hash: str | None = None) -> list[dict]:
        """Read-only diagnostic projection for held versioned registrations."""
        registrations = [item for events in self.registrations.values() for item in events
                         if item.get("event") == "candidate_registration"]
        links = set(self._identity_link_group_index)
        by_source_digest: dict[tuple[str, str], list[dict]] = {}
        by_source_title: dict[tuple[str, str], list[dict]] = {}
        by_extraction_digest: dict[tuple[str, str, str], list[dict]] = {}
        digest_by_object: dict[int, str] = {}
        for registration in registrations:
            source_value = str(registration.get("source_hash", ""))
            content_digest = (registration.get("logical_identity_digest")
                              or logical_recipe_identity(registration.get("candidate", {})))
            title_value = str(registration.get("candidate", {}).get("name", "")).casefold()
            by_source_digest.setdefault((source_value, content_digest), []).append(registration)
            by_source_title.setdefault((source_value, title_value), []).append(registration)
            by_extraction_digest.setdefault((source_value, str(registration.get("extraction_id", "")),
                                              content_digest), []).append(registration)
            digest_by_object[id(registration)] = str(content_digest)
        protected_source_cache: dict[str, bool] = {}
        output = []
        for held in registrations:
            held_id = str(held.get("candidate_id", ""))
            digest = digest_by_object[id(held)]
            key = (held_id, held.get("source_hash"), held.get("extraction_id"))
            if held.get("identity_status") != "needs_reconciliation" or key in links:
                continue
            if source_hash and held.get("source_hash") != source_hash:
                continue
            source_hash_value = str(held.get("source_hash", ""))
            matches = []
            matches = [item for item in by_source_digest.get((source_hash_value, str(digest)), [])
                       if item is not held]
            groups: dict[str, list[dict]] = {}
            unlinked_conflict = False
            registered_matches = []
            for prior in matches:
                link_group = self._identity_link_group_index.get((
                    str(prior.get("candidate_id", "")), source_hash_value,
                    str(prior.get("extraction_id", "")),
                ))
                group_id = str(prior.get("identity_group_id") or link_group or "")
                if not group_id and prior.get("identity_status") == "registered":
                    group_id = str(prior.get("candidate_id", ""))
                if not group_id:
                    unlinked_conflict = True
                else:
                    groups.setdefault(group_id, []).append(prior)
                    registered_matches.append(prior)
            current_siblings = by_extraction_digest.get(
                (source_hash_value, str(held.get("extraction_id", "")), str(digest)), [])
            if source_hash_value not in protected_source_cache:
                protected_source_cache[source_hash_value] = self._source_has_protected_import_history(
                    source_hash_value)
            source_protected = protected_source_cache[source_hash_value]
            protected = source_protected
            eligible = (len(groups) == 1 and not unlinked_conflict and len(current_siblings) == 1
                        and len(registered_matches) >= 1 and not protected
                        and source_hash_value and held.get("extraction_id"))
            prior = (max([item for item in next(iter(groups.values()), [])
                          if item.get("identity_status") == "registered" or self._identity_link_group_index.get((
                              str(item.get("candidate_id", "")), source_hash_value,
                              str(item.get("extraction_id", ""))))],
                         key=lambda item: str(item.get("timestamp", ""))) if groups else None)
            title = str(held.get("candidate", {}).get("name", ""))
            title_matches = [item for item in by_source_title.get((source_hash_value, title.casefold()), [])
                             if item is not held]
            possible = []
            for item in (matches or title_matches)[:8]:
                previous_candidate = item.get("candidate", {})
                item_link_group = self._identity_link_group_index.get((
                    str(item.get("candidate_id", "")), source_hash_value,
                    str(item.get("extraction_id", "")),
                ))
                possible.append({
                    "candidate_id": item.get("candidate_id"), "extraction_id": item.get("extraction_id"),
                    "identity_status": ("registered" if item.get("identity_status") == "registered"
                                         or item_link_group else item.get("identity_status")),
                    "differences": sorted(authoritative_recipe_differences(
                        held.get("candidate", {}), previous_candidate)),
                    "suggestions_differ": held.get("candidate", {}).get("suggestions", [])
                                          != previous_candidate.get("suggestions", []),
                    "candidate": previous_candidate,
                    "history": self._candidate_history_counts(str(item.get("candidate_id", ""))),
                    "protected": source_protected,
                })
            output.append({
                "candidate_id": held_id, "source_hash": source_hash_value,
                "source": held.get("source", ""), "extraction_id": held.get("extraction_id", ""),
                "candidate": held.get("candidate", {}), "fingerprint": held.get("fingerprint", ""),
                "identity_digest": digest, "authoritative_differences": sorted(
                    authoritative_recipe_differences(held.get("candidate", {}),
                                                     prior.get("candidate", {}) if prior else {})),
                "suggestions_differ": bool(prior and held.get("candidate", {}).get("suggestions", [])
                                           != prior.get("candidate", {}).get("suggestions", [])),
                "diagnostics_differ": bool(prior and any(
                    held.get("candidate", {}).get(field) != prior.get("candidate", {}).get(field)
                    for field in ("missing_information", "warnings", "confidence", "uncertainties",
                                  "source_method", "source_line_ranges"))),
                "previous_candidate_id": prior.get("candidate_id") if prior else None,
                "previous_extraction_id": prior.get("extraction_id") if prior else None,
                "previous_candidate": prior.get("candidate", {}) if prior else None,
                "previous_history": self._candidate_history_counts(str(prior.get("candidate_id", "")))
                                    if prior else {},
                "held_history": self._candidate_history_counts(held_id),
                "possible_matches": possible, "protected": protected,
                "eligible": bool(eligible),
                "reason": ("A protected Mealie outcome exists for this source."
                           if protected else "" if eligible
                           else "A unique authoritative-content match could not be established."),
            })
        return output

    def _candidate_history_counts(self, candidate_id: str) -> dict[str, int]:
        return dict(self._candidate_history_index.get(candidate_id, {}))

    def confirm_identity_match(self, held_candidate_id: str, previous_candidate_id: str,
                               evidence: str = "") -> dict:
        """Append an explicit, idempotent link without changing either candidate stream."""
        with _state_lock(self.path):
            self._reload()
            operation_id = f"identity-confirm:{held_candidate_id}:{previous_candidate_id}"
            existing = next((item for item in self.events
                             if item.get("operation_id") == operation_id), None)
            if existing:
                if (existing.get("event") != "candidate_identity_link"
                        or existing.get("previous_candidate_id") != previous_candidate_id):
                    raise ReviewConflict("identity confirmation operation ID was already used")
                if self.identity_link_status(held_candidate_id, str(existing.get("source_hash", "")),
                                             str(existing.get("extraction_id", ""))) != "registered":
                    raise ReviewConflict("saved identity link is incomplete or conflicts with its registrations")
                return existing
            proposal = next((item for item in self.identity_reconciliation_candidates()
                             if item.get("candidate_id") == held_candidate_id), None)
            if not proposal:
                raise ReviewConflict("this identity match is no longer safe to confirm")
            safe_pair = (proposal.get("previous_candidate_id") == previous_candidate_id
                         and proposal.get("eligible"))
            selected_matches = [item for item in proposal.get("possible_matches", [])
                                if item.get("candidate_id") == previous_candidate_id
                                and not item.get("differences")
                                and item.get("identity_status") == "registered"]
            selected_match = selected_matches[0] if len(selected_matches) == 1 else None
            user_selected = bool(selected_match and evidence.strip() and not proposal.get("protected"))
            if not (safe_pair or user_selected):
                raise ReviewConflict("this identity match is no longer safe to confirm")
            expected_previous_extraction = (
                str(proposal.get("previous_extraction_id") or "") if safe_pair
                else str(selected_match.get("extraction_id") or "")
            )
            event = self._base_event("candidate_identity_link", held_candidate_id,
                                     proposal["source_hash"], proposal["extraction_id"], operation_id)
            previous_matches = [item for item in self.registrations.get(previous_candidate_id, [])
                                if item.get("source_hash") == proposal["source_hash"]
                                and logical_recipe_identity(item.get("candidate", {}))
                                == proposal["identity_digest"]
                                and (not expected_previous_extraction
                                     or item.get("extraction_id") == expected_previous_extraction)]
            if len(previous_matches) != 1:
                raise ReviewConflict("the selected prior registration is ambiguous or no longer matches")
            previous = previous_matches[0]
            event.update({"previous_candidate_id": previous_candidate_id,
                          "previous_extraction_id": str(previous.get("extraction_id", "")),
                          "identity_group_id": str(previous.get("identity_group_id") or previous_candidate_id),
                          "logical_identity_digest": proposal["identity_digest"],
                          "comparison_version": 1,
                          "decision": ("confirmed_unique_content_match" if safe_pair
                                       else "confirmed_user_selected_match"),
                          "evidence": evidence.strip() if user_selected and not safe_pair else ""})
            expected = {key: value for key, value in event.items()
                        if key not in {"event_id", "timestamp", "actor"}}
            prior = self._existing_operation(operation_id, "candidate_identity_link", expected)
            if prior:
                return prior
            self._append(event, locked=True)
            self._reload()
            return self._readback(event)

    def defer_identity_match(self, held_candidate_id: str, extraction_id: str) -> dict:
        operation_id = f"identity-defer:{held_candidate_id}:{extraction_id}"
        with _state_lock(self.path):
            self._reload()
            registration = next((item for item in self.registrations.get(held_candidate_id, [])
                                 if item.get("extraction_id") == extraction_id
                                 and item.get("identity_status") == "needs_reconciliation"), None)
            if not registration:
                raise ReviewConflict("identity candidate is no longer held")
            event = self._base_event("candidate_identity_review", held_candidate_id,
                                     registration.get("source_hash", ""), extraction_id, operation_id)
            event.update({"decision": "deferred", "comparison_version": 1})
            expected = {key: value for key, value in event.items()
                        if key not in {"event_id", "timestamp", "actor"}}
            prior = self._existing_operation(operation_id, "candidate_identity_review", expected)
            if prior:
                return prior
            self._append(event, locked=True)
            self._reload()
            return self._readback(event)

    def _exact_registration_batch(self, source_hash: str, extraction_id: str,
                                  candidates: list[RecipeCandidate]) -> list[dict] | None:
        """Return stable registrations from this snapshot, or require a fresh locked read."""
        occurrences: dict[str, int] = {}
        output = []
        for candidate in candidates:
            fingerprint = hashlib.sha256(_canonical(_candidate_payload(candidate)).encode()).hexdigest()
            occurrence = occurrences.get(fingerprint, 0)
            occurrences[fingerprint] = occurrence + 1
            matches = self._exact_registration_index.get(
                (source_hash, extraction_id, fingerprint), [],
            )
            if not matches:
                # Accept records written after the notes field was introduced,
                # where an empty notes list was serialized into the fingerprint.
                legacy_payload = candidate.model_dump(mode="json")
                legacy_fingerprint = hashlib.sha256(_canonical(legacy_payload).encode()).hexdigest()
                matches = self._exact_registration_index.get(
                    (source_hash, extraction_id, legacy_fingerprint), [],
                )
            if (occurrence != 0 or len(matches) != 1
                    or matches[0].get("identity_status") != "registered"):
                return None
            output.append(matches[0])
        return output

    def _index_registration(self, event: dict) -> None:
        key = (str(event.get("source_hash", "")), str(event.get("extraction_id", "")),
               str(event.get("fingerprint", "")))
        self._exact_registration_index.setdefault(key, []).append(event)

    def _source_has_protected_import_history(self, source_hash: str) -> bool:
        if not source_hash:
            return False
        if self._protected_source_cache is None:
            self._protected_source_cache = self._load_protected_source_hashes()
        return source_hash in self._protected_source_cache

    def _load_protected_source_hashes(self) -> set[str]:
        try:
            if self.state_path.exists():
                ui_count("ledger_reads")
            state_records = ([json.loads(line) for line in self.state_path.read_text(encoding="utf-8").splitlines()
                              if line.strip()] if self.state_path.exists() else [])
        except (OSError, json.JSONDecodeError) as exc:
            raise ReviewConflict("import state is unreadable; new identities remain held") from exc
        protected = set()
        for record in state_records:
            if not isinstance(record, dict) or record.get("status") not in {
                "IMPORTED", "UNCERTAIN", "NEEDS_MANUAL_CHECK",
            }:
                continue
            source_hash = str(record.get("source_hash", ""))
            if source_hash:
                protected.add(source_hash)
            key_hash = str(record.get("key", "")).partition(":")[0]
            if key_hash:
                protected.add(key_hash)

        event_path = self.path.parent / "ui_import_events.jsonl"
        try:
            if event_path.exists():
                ui_count("ledger_reads")
            events = ([json.loads(line) for line in event_path.read_text(encoding="utf-8").splitlines()
                       if line.strip()] if event_path.exists() else [])
        except (OSError, json.JSONDecodeError) as exc:
            raise ReviewConflict("import history is unreadable; new identities remain held") from exc
        attempts: dict[str, str] = {}
        for event in events:
            if not isinstance(event, dict):
                continue
            source_hash = str(event.get("source_hash", ""))
            if not source_hash:
                continue
            attempt_id = str(event.get("attempt_id", ""))
            kind = event.get("event")
            if kind == "intent_started" and attempt_id:
                attempts[(source_hash, attempt_id)] = "started"
            elif kind == "create_rejected" and attempt_id:
                attempts[(source_hash, attempt_id)] = "rejected"
            elif kind in {"create_uncertain", "creation_confirmed", "reconciled_existing"}:
                protected.add(source_hash)
        protected.update(source for (source, _attempt), status in attempts.items() if status == "started")
        return protected

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
        payload = _candidate_payload(candidate)
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

    def create_suggestion_request(self, *, candidate_id: str, source_hash: str,
                                  extraction_id: str, revision: int, content_digest: str,
                                  recipe_snapshot: dict, suggestion_type: str,
                                  operation_id: str, consent: bool,
                                  target_bindings: list[dict] | None = None) -> dict:
        """Durably record an explicitly authorized, exact-revision text request."""
        if not consent:
            raise ValueError("explicit cloud consent is required")
        if suggestion_type != "instructions":
            raise ValueError("unsupported suggestion type")
        if (set(recipe_snapshot) != {"name", "ingredients", "instructions"}
                or not isinstance(recipe_snapshot.get("name"), str)
                or not isinstance(recipe_snapshot.get("ingredients"), list)
                or not isinstance(recipe_snapshot.get("instructions"), list)):
            raise ValueError("suggestion requests may include only recipe title, ingredients, and instructions")
        request_id = hashlib.sha256(
            f"{candidate_id}\0{source_hash}\0{extraction_id}\0{revision}\0{suggestion_type}\0{operation_id}".encode()
        ).hexdigest()
        with _state_lock(self.path):
            self._reload()
            registration, identity_status, protected = self._effective_identity_authority(
                candidate_id, source_hash, extraction_id, check_import_protection=True)
            if not registration or identity_status != "registered":
                raise ReviewConflict("candidate identity is not ready for a suggestion request")
            if protected:
                raise ReviewConflict("protected recipes cannot request generated suggestions")
            latest = max((item for item in self.revisions.get(candidate_id, [])
                          if item.get("source_hash") == source_hash
                          and item.get("extraction_id") == extraction_id),
                         key=lambda item: int(item["revision"]), default=None)
            if (latest is None or int(latest["revision"]) != int(revision)
                    or latest.get("content_sha256") != content_digest):
                raise ReviewConflict("suggestion request must match the current saved recipe revision")
            latest_targets = latest.get("target_bindings", [])
            requested_targets = []
            for target in (target_bindings or []):
                match = next((item for item in latest_targets
                              if item.get("target_id") == target.get("target_id")
                              and item.get("field") == "instructions"), None)
                if (not match or not isinstance(match.get("position"), int)
                        or match["position"] < 0 or match["position"] >= len(recipe_snapshot["instructions"])):
                    raise ReviewConflict("suggestion target is not bound to this recipe revision")
                requested_targets.append({"target_id": match["target_id"], "field": "instructions",
                                          "position": match["position"],
                                          "current_value": recipe_snapshot["instructions"][match["position"]]})
            prior_request = self._suggestion_requests_by_id.get(request_id)
            if prior_request:
                return self._suggestion_request_loaded(request_id)
            existing = next((item for item in reversed(
                self._suggestion_requests_by_candidate.get(candidate_id, []))
                             if item.get("source_hash") == source_hash
                             and item.get("extraction_id") == extraction_id
                             and item.get("revision") == int(revision)
                             and item.get("suggestion_type") == suggestion_type), None)
            if existing:
                result = self._suggestion_request_loaded(existing["request_id"]) or dict(existing)
                status = result.get("status", "queued")
                if status in {"queued", "running"}:
                    return result
                if status == "completed":
                    proposals = [item for item in self._dynamic_suggestions_by_candidate.get(candidate_id, [])
                                 if item.get("request_id") == existing["request_id"]]
                    terminal_decisions = {
                        "accepted", "edited", "declined", "rejected", "stale_discarded",
                    }
                    unresolved = any(
                        item.get("status") in {"pending", "stale", "target_missing"}
                        and self._dynamic_decisions_by_candidate.get(candidate_id, {})
                        .get(item.get("proposal_id"), {}).get("disposition") not in terminal_decisions
                        for item in proposals
                    )
                    if unresolved:
                        return result
            event = self._base_event("suggestion_request", candidate_id, source_hash,
                                     extraction_id, operation_id)
            event.update({"request_id": request_id, "revision": int(revision),
                          "content_digest": content_digest,
                          "recipe_snapshot_digest": _recipe_digest(recipe_snapshot),
                          "instruction_targets": requested_targets,
                          "recipe_snapshot": json.loads(_canonical(recipe_snapshot)),
                          "suggestion_type": suggestion_type, "consent": True,
                          "status": "queued", "prompt_version": "instruction-suggestion-v1",
                          "schema_version": "instruction-suggestion-v1"})
            self._append(event, locked=True)
            self._reload()
            return self._readback(event)

    def get_suggestion_request(self, request_id: str) -> dict | None:
        with _state_lock(self.path):
            self._reload()
            return self._suggestion_request_loaded(request_id)

    def _suggestion_request_loaded(self, request_id: str) -> dict | None:
        request = self._suggestion_requests_by_id.get(request_id)
        if not request:
            return None
        return dict(self._suggestion_request_status_by_id.get(request_id, request))

    def latest_suggestion_request_loaded(self, candidate_id: str, source_hash: str,
                                         extraction_id: str) -> dict | None:
        """Return the newest request from this store snapshot without reloading."""
        request = next((item for item in reversed(
            self._suggestion_requests_by_candidate.get(candidate_id, []))
                        if item.get("source_hash") == source_hash
                        and item.get("extraction_id") == extraction_id), None)
        return self._suggestion_request_loaded(request["request_id"]) if request else None

    def latest_suggestion_request(self, candidate_id: str, source_hash: str,
                                  extraction_id: str) -> dict | None:
        """Return the newest durable request for a candidate/source/extraction tuple."""
        with _state_lock(self.path):
            self._reload()
            return self.latest_suggestion_request_loaded(candidate_id, source_hash, extraction_id)

    def claim_suggestion_request(self, request_id: str, worker_claim_id: str) -> dict | None:
        """Atomically claim queued work so only one process can call the provider."""
        with _state_lock(self.path):
            self._reload()
            request = next((item for item in self.events if item.get("event") == "suggestion_request"
                            and item.get("request_id") == request_id), None)
            if not request:
                raise ReviewConflict("suggestion request was not found")
            if self._suggestion_request_status_loaded(request_id) != "queued":
                return self._suggestion_request_loaded(request_id)
            event = self._base_event("suggestion_request_status", request["candidate_id"],
                                     request["source_hash"], request["extraction_id"],
                                     f"{request_id}:claim:{uuid.uuid4().hex}")
            event.update({"request_id": request_id, "status": "running", "error": "",
                          "model_id": "", "worker_host": socket.gethostname(),
                          "worker_pid": os.getpid(), "worker_claim_id": worker_claim_id})
            self._append(event, locked=True)
            self._reload()
            return self._suggestion_request_loaded(request_id)

    def recover_interrupted_suggestion_requests(self) -> list[str]:
        """Mark running requests interrupted only when their claimed worker is gone."""
        recovered = []
        with _state_lock(self.path):
            self._reload()
            requests = [item for item in self.events if item.get("event") == "suggestion_request"]
            for request in requests:
                request_id = request.get("request_id", "")
                if self._suggestion_request_status_loaded(request_id) != "running":
                    continue
                latest = self._suggestion_request_loaded(request_id) or {}
                host = latest.get("worker_host", "")
                pid = latest.get("worker_pid")
                if host and host != socket.gethostname():
                    # A different host may still own the provider call; never replay it here.
                    continue
                alive = False
                if host == socket.gethostname() and isinstance(pid, int) and pid > 0:
                    alive = _process_is_alive(pid)
                if alive:
                    continue
                event = self._base_event("suggestion_request_status", request["candidate_id"],
                                         request["source_hash"], request["extraction_id"],
                                         f"{request_id}:interrupted:{uuid.uuid4().hex}")
                event.update({"request_id": request_id, "status": "interrupted",
                              "error": "Worker stopped during provider request; create a new authorized request.",
                              "model_id": ""})
                self._append(event, locked=True)
                recovered.append(request_id)
            if recovered:
                self._reload()
        return recovered

    def update_suggestion_request(self, request_id: str, status: str, *, error: str = "",
                                  model_id: str = "", locked: bool = False) -> dict:
        allowed = {"running", "completed", "failed", "quota_blocked", "interrupted", "cancelled", "stale"}
        if status not in allowed:
            raise ValueError("unsupported suggestion request status")
        context = nullcontext() if locked else _state_lock(self.path)
        with context:
            if not locked:
                self._reload()
            request = next((item for item in self.events if item.get("request_id") == request_id
                            and item.get("event") == "suggestion_request"), None)
            if not request:
                raise ReviewConflict("suggestion request was not found")
            current_status = next((item.get("status") for item in reversed(self.events)
                                   if item.get("event") == "suggestion_request_status"
                                   and item.get("request_id") == request_id), request.get("status", "queued"))
            if status == "running" and current_status != "queued":
                raise ReviewConflict(f"suggestion request cannot start from {current_status}")
            if status == "cancelled" and current_status not in {"queued", "running"}:
                raise ReviewConflict(f"suggestion request cannot be cancelled from {current_status}")
            if status not in {"running", "cancelled"} and current_status != "running":
                raise ReviewConflict(f"suggestion request cannot finish from {current_status}")
            event = self._base_event("suggestion_request_status", request["candidate_id"],
                                     request["source_hash"], request["extraction_id"],
                                     f"{request_id}:{status}:{uuid.uuid4().hex}")
            event.update({"request_id": request_id, "status": status,
                          "error": str(error)[:300], "model_id": model_id})
            self._append(event, locked=True)
            self._reload()
            return event

    def cancel_suggestion_request(self, request_id: str) -> dict:
        """Durably cancel queued/running work; publication uses the same ledger lock."""
        with _state_lock(self.path):
            self._reload()
            request = next((item for item in self.events if item.get("event") == "suggestion_request"
                            and item.get("request_id") == request_id), None)
            if not request:
                raise ReviewConflict("suggestion request was not found")
            current_status = self._suggestion_request_status_loaded(request_id)
            if current_status not in {"queued", "running"}:
                result = dict(request)
                result["status"] = current_status
                return result
            event = self._base_event("suggestion_request_status", request["candidate_id"],
                                     request["source_hash"], request["extraction_id"],
                                     f"{request_id}:cancel:{uuid.uuid4().hex}")
            event.update({"request_id": request_id, "status": "cancelled",
                          "error": "Cancelled by the reviewer.", "model_id": ""})
            self._append(event, locked=True)
            self._reload()
            return event

    def record_dynamic_proposals(self, request_id: str, proposals: list[dict]) -> list[dict]:
        with _state_lock(self.path):
            self._reload()
            request = next((item for item in self.events if item.get("request_id") == request_id
                            and item.get("event") == "suggestion_request"), None)
            if not request:
                raise ReviewConflict("suggestion request was not found")
            current_status = next((item.get("status") for item in reversed(self.events)
                                   if item.get("event") == "suggestion_request_status"
                                   and item.get("request_id") == request_id), request.get("status", "queued"))
            if current_status != "running":
                raise ReviewConflict("only an active suggestion request can publish proposals")
            latest = max((item for item in self.revisions.get(request["candidate_id"], [])
                          if item.get("source_hash") == request["source_hash"]
                          and item.get("extraction_id") == request["extraction_id"]),
                         key=lambda item: int(item["revision"]), default=None)
            stale = (latest is None or int(latest["revision"]) != int(request["revision"])
                     or latest.get("content_sha256") != request["content_digest"])
            prepared = []
            for index, raw in enumerate(proposals):
                proposal = dict(raw)
                required = ("field_path", "proposed_value", "classification", "rationale", "uncertainty")
                if any(not str(proposal.get(key, "")).strip() for key in required):
                    raise ValueError("proposal is missing required structured fields")
                if proposal["classification"] != "inferred":
                    raise ValueError("invalid suggestion classification")
                location = proposal.get("insertion_location")
                is_bound_step = bool(proposal.get("target_id") and any(
                    item.get("target_id") == proposal["target_id"]
                    for item in request.get("instruction_targets", [])))
                if proposal.get("target_id") and not is_bound_step:
                    raise ValueError("proposal target is not bound to the requested recipe revision")
                if ((str(proposal["field_path"]) != "instructions" and not re.fullmatch(
                        r"instructions\[\d+\]", str(proposal["field_path"])))
                        or (not is_bound_step and not isinstance(location, dict))):
                    raise ValueError("instruction proposal needs a mapped target or explicit insertion location")
                proposal_id = hashlib.sha256(
                    f"{request_id}\0{index}\0{_canonical(proposal)}".encode("utf-8")
                ).hexdigest()
                event = self._base_event("dynamic_suggestion", request["candidate_id"],
                                         request["source_hash"], request["extraction_id"],
                                         f"proposal:{proposal_id}")
                event.update({"request_id": request_id, "proposal_id": proposal_id,
                              "revision": request["revision"], "content_digest": request["content_digest"],
                              "field_path": str(proposal["field_path"]),
                              "target_id": str(proposal.get("target_id", "")),
                              "insertion_location": proposal.get("insertion_location"),
                              "proposed_value": str(proposal["proposed_value"]),
                              "classification": proposal["classification"],
                              "rationale": str(proposal["rationale"]),
                              "uncertainty": str(proposal["uncertainty"]),
                              "model_id": str(proposal.get("model_id", "")),
                              "prompt_version": request["prompt_version"],
                              "schema_version": request["schema_version"],
                              "status": "stale" if stale else "pending"})
                prepared.append(event)
            current_status = self._suggestion_request_status_loaded(request_id)
            if current_status != "running":
                raise ReviewConflict(f"suggestion request cannot publish from {current_status}")
            missing = [event for event in prepared
                       if not any(item.get("proposal_id") == event["proposal_id"] for item in self.events)]
            status_event = self._base_event("suggestion_request_status", request["candidate_id"],
                                            request["source_hash"], request["extraction_id"],
                                            f"{request_id}:publish:{uuid.uuid4().hex}")
            status_event.update({"request_id": request_id,
                                 "status": "stale" if stale else "completed",
                                 "error": "", "model_id": ""})
            self._append_many([*missing, status_event], locked=True)
            self._reload()
            return [self._readback(event) for event in prepared]

    def dynamic_suggestions_for(self, candidate_id: str, source_hash: str,
                                extraction_id: str) -> list[dict]:
        with _state_lock(self.path):
            self._reload()
            return [dict(item) for item in self._dynamic_suggestions_by_candidate.get(candidate_id, [])
                    if item.get("source_hash") == source_hash
                    and item.get("extraction_id") == extraction_id]

    def _suggestion_request_status_loaded(self, request_id: str) -> str:
        request = self._suggestion_request_status_by_id.get(request_id)
        return str(request.get("status", "queued")) if request else "queued"

    def resolve_dynamic_suggestion(self, *, candidate_id: str, proposal_id: str,
                                   disposition: str, final_value: str | None = None,
                                   operation_id: str) -> dict:
        if disposition not in {"accepted", "edited", "declined", "rejected"}:
            raise ValueError("unsupported generated suggestion decision")
        with _state_lock(self.path):
            self._reload()
            proposal = next((item for item in self.events if item.get("event") == "dynamic_suggestion"
                             and item.get("candidate_id") == candidate_id
                             and item.get("proposal_id") == proposal_id), None)
            if not proposal:
                raise ReviewConflict("generated proposal was not found")
            if proposal.get("status") != "pending":
                raise ReviewConflict("proposal is stale or already resolved")
            latest = max((item for item in self.revisions.get(candidate_id, [])
                          if item.get("source_hash") == proposal["source_hash"]
                          and item.get("extraction_id") == proposal["extraction_id"]),
                         key=lambda item: int(item["revision"]), default=None)
            if (latest is None or int(latest["revision"]) != int(proposal["revision"])
                    or latest.get("content_sha256") != proposal["content_digest"]):
                raise ReviewConflict("recipe changed; proposal is stale and requires an explicit rebase decision")
            if disposition in {"accepted", "edited"} and not str(final_value or proposal["proposed_value"]).strip():
                raise ValueError("accepted suggestion content cannot be empty")
            if disposition in {"accepted", "edited"}:
                location = proposal.get("insertion_location") or {}
                values = latest.get("recipe", {}).get("instructions")
                target = next((item for item in latest.get("target_bindings", [])
                               if item.get("target_id") == proposal.get("target_id")
                               and item.get("field") == "instructions"), None)
                position = target.get("position") if target else None
                valid_target = isinstance(values, list) and isinstance(position, int) and 0 <= position < len(values)
                index = location.get("index")
                valid_insert = (isinstance(values, list) and location.get("field") == "instructions"
                                and isinstance(index, int) and 0 <= index <= len(values))
                valid_empty_append = (isinstance(values, list) and not values
                                      and location.get("field") == "instructions"
                                      and location.get("after") is None)
                if not (valid_target or valid_insert or valid_empty_append):
                    raise ReviewConflict("proposal target is missing or changed; it cannot be applied")
            event = self._base_event("dynamic_suggestion_decision", candidate_id,
                                     proposal["source_hash"], proposal["extraction_id"], operation_id)
            event.update({"proposal_id": proposal_id, "request_id": proposal["request_id"],
                          "revision": proposal["revision"], "content_digest": proposal["content_digest"],
                          "disposition": disposition, "final_value": final_value})
            expected = {k: v for k, v in event.items() if k not in {"event_id", "timestamp", "actor"}}
            prior = self._existing_operation(operation_id, "dynamic_suggestion_decision", expected)
            if prior:
                return prior
            self._append(event, locked=True)
            self._reload()
            return self._readback(event)

    def discard_stale_dynamic_suggestion(self, *, candidate_id: str, proposal_id: str,
                                          operation_id: str) -> dict:
        """Record review of a stale result without applying or rebasing its content."""
        with _state_lock(self.path):
            self._reload()
            proposal = next((item for item in self.events if item.get("event") == "dynamic_suggestion"
                             and item.get("candidate_id") == candidate_id
                             and item.get("proposal_id") == proposal_id), None)
            if not proposal:
                raise ReviewConflict("generated proposal was not found")
            latest = max((item for item in self.revisions.get(candidate_id, [])
                          if item.get("source_hash") == proposal["source_hash"]
                          and item.get("extraction_id") == proposal["extraction_id"]),
                         key=lambda item: int(item["revision"]), default=None)
            if (latest is None or (int(latest["revision"]) == int(proposal["revision"])
                                   and latest.get("content_sha256") == proposal.get("content_digest"))):
                raise ReviewConflict("proposal is not stale")
            event = self._base_event("dynamic_suggestion_decision", candidate_id,
                                     proposal["source_hash"], proposal["extraction_id"], operation_id)
            event.update({"proposal_id": proposal_id, "request_id": proposal["request_id"],
                          "base_revision": proposal["revision"], "revision": latest["revision"],
                          "current_content_digest": latest["content_sha256"],
                          "content_digest": proposal["content_digest"],
                          "disposition": "stale_discarded", "final_value": None})
            expected = {k: v for k, v in event.items() if k not in {"event_id", "timestamp", "actor"}}
            prior = self._existing_operation(operation_id, "dynamic_suggestion_decision", expected)
            if prior:
                return prior
            if any(item.get("event") == "dynamic_suggestion_decision"
                   and item.get("proposal_id") == proposal_id for item in self.events):
                raise ReviewConflict("proposal already has a recorded decision")
            self._append(event, locked=True)
            self._reload()
            return self._readback(event)

    def rebase_and_apply_dynamic_suggestion(self, *, candidate_id: str, proposal_id: str,
                                            expected_parent: int, expected_content_digest: str,
                                            recipe: dict, target_bindings: list[dict],
                                            disposition: str, final_value: str | None,
                                            source_path: str | Path, confirmed: bool,
                                            insertion_location: dict | None = None,
                                            operation_id: str) -> tuple[dict, dict]:
        """Explicitly compare a stale proposal against the current revision and apply it atomically."""
        if not confirmed:
            raise ValueError("explicit comparison and rebase confirmation is required")
        if disposition not in {"accepted", "edited"}:
            raise ValueError("rebase application requires accepted or edited disposition")
        value = str(final_value or "").strip()
        if not value:
            raise ValueError("accepted suggestion content cannot be empty")
        with _state_lock(self.path):
            self._reload()
            proposal = next((item for item in self.events if item.get("event") == "dynamic_suggestion"
                             and item.get("candidate_id") == candidate_id
                             and item.get("proposal_id") == proposal_id), None)
            if not proposal:
                raise ReviewConflict("generated proposal was not found")
            proposal_decision = next((item for item in self.events
                                      if item.get("event") == "dynamic_suggestion_decision"
                                      and item.get("proposal_id") == proposal_id), None)
            if proposal_decision:
                prior_revision = next((item for item in self.events
                                       if item.get("event") == "review_revision"
                                       and item.get("operation_id") == f"{operation_id}:revision"), None)
                prior_decision = next((item for item in self.events
                                       if item.get("event") == "dynamic_suggestion_decision"
                                       and item.get("operation_id") == f"{operation_id}:decision"), None)
                if (prior_revision and prior_decision
                        and prior_decision.get("proposal_id") == proposal_id
                        and prior_decision.get("final_value") == value
                        and int(prior_revision.get("parent_revision", -1)) == int(expected_parent)
                        and prior_decision.get("rebase_to_digest") == expected_content_digest):
                    replay_recipe = json.loads(_canonical(recipe))
                    replay_recipe["suggestions"] = prior_revision.get("recipe", {}).get("suggestions", [])
                    if _recipe_digest(replay_recipe) == prior_revision.get("content_sha256"):
                        return self._readback(prior_revision), self._readback(prior_decision)
                raise ReviewConflict("proposal already has a recorded decision")
            if proposal.get("status") not in {"pending", "stale"}:
                raise ReviewConflict("proposal is not available for explicit rebase")
            registration, identity_status, protected = self._effective_identity_authority(
                candidate_id, str(proposal.get("source_hash", "")),
                str(proposal.get("extraction_id", "")), check_import_protection=True)
            if not registration or identity_status != "registered":
                raise ReviewConflict("candidate identity is not ready for rebase")
            if protected:
                raise ReviewConflict("protected recipes cannot apply generated suggestions")
            latest = max((item for item in self.revisions.get(candidate_id, [])
                          if item.get("source_hash") == proposal["source_hash"]
                          and item.get("extraction_id") == proposal["extraction_id"]),
                         key=lambda item: int(item["revision"]), default=None)
            if (latest is None or int(latest["revision"]) != int(expected_parent)
                    or latest.get("content_sha256") != expected_content_digest):
                raise ReviewConflict("recipe changed during comparison; reload and compare again")
            if (int(latest["revision"]) == int(proposal["revision"])
                    and latest.get("content_sha256") == proposal.get("content_digest")):
                raise ReviewConflict("proposal is not stale and does not need a rebase")
            try:
                actual_source_hash = hashlib.sha256(Path(source_path).read_bytes()).hexdigest()
            except OSError as exc:
                raise ReviewConflict("source scan is unavailable; stale proposal cannot be rebased") from exc
            if actual_source_hash != proposal["source_hash"]:
                raise ReviewConflict("source scan bytes changed; stale proposal cannot be rebased")

            effective_current = self.project_loaded(
                candidate_id, proposal["source_hash"], proposal["extraction_id"],
                RecipeCandidate.model_validate(latest["recipe"]),
            )
            current_recipe = json.loads(_canonical(effective_current.recipe))
            current_instructions = current_recipe.get("instructions")
            if not isinstance(current_instructions, list):
                raise ReviewConflict("current instructions are unavailable for comparison")
            current_targets = json.loads(_canonical(latest.get("target_bindings", [])))
            current_rows = [item for item in current_targets
                            if item.get("field") in {"ingredients", "instructions"}
                            and isinstance(item.get("position"), int)]
            provided_rows = [item for item in target_bindings
                             if item.get("field") in {"ingredients", "instructions"}
                             and isinstance(item.get("position"), int)]

            def row_identity(item):
                return (str(item.get("target_id", "")), item.get("field"), item.get("position"))

            current_identities = {row_identity(item) for item in current_rows}
            provided_identities = [row_identity(item) for item in provided_rows]
            if (any(not identity[0] for identity in provided_identities)
                    or len(set(provided_identities)) != len(provided_identities)
                    or len({identity[0] for identity in provided_identities}) != len(provided_identities)):
                raise ReviewConflict("target bindings contain a missing or duplicate row identity")
            target_id = str(proposal.get("target_id", ""))
            comparison_current_value = None
            confirmed_location = None
            canonical_targets = current_targets
            if target_id:
                target = next((item for item in latest.get("target_bindings", [])
                               if item.get("target_id") == target_id
                               and item.get("field") == "instructions"), None)
                if not target:
                    raise ReviewConflict("proposal target was removed or changed; it cannot be rebased")
                if set(provided_identities) != current_identities:
                    raise ReviewConflict("rebasing a target must preserve all current row bindings exactly")
                supplied = next((item for item in target_bindings
                                 if item.get("target_id") == target_id
                                 and item.get("field") == "instructions"), None)
                position = target.get("position") if target else None
                if (not target or not supplied or supplied.get("position") != position
                        or not isinstance(position, int) or position < 0 or position >= len(current_instructions)):
                    raise ReviewConflict("proposal target was removed or changed; it cannot be rebased")
                comparison_current_value = current_instructions[position]
                expected_recipe = json.loads(_canonical(current_recipe))
                expected_recipe["instructions"][position] = value
                submitted_recipe = json.loads(_canonical(recipe))
                # Suggestion review statuses are projection metadata. Keep the
                # exact current projection in the revision while comparing all
                # authoritative recipe fields strictly.
                submitted_recipe["suggestions"] = expected_recipe.get("suggestions", [])
                if _canonical(submitted_recipe) != _canonical(expected_recipe):
                    fields = sorted(key for key in set(submitted_recipe) | set(expected_recipe)
                                    if _canonical(submitted_recipe.get(key)) != _canonical(expected_recipe.get(key)))
                    raise ReviewConflict("rebased recipe must preserve current content; changed fields: "
                                         + ", ".join(fields))
                recipe = expected_recipe
                digest = _recipe_digest(recipe)
                for binding in canonical_targets:
                    if binding.get("target_id") == target_id and binding.get("field") == "instructions":
                        if "value_sha256" in binding:
                            binding["value_sha256"] = hashlib.sha256(value.encode("utf-8")).hexdigest()
            else:
                if (len(provided_identities) != len(current_identities) + 1
                        or not current_identities.issubset(set(provided_identities))):
                    raise ReviewConflict("rebasing an insertion must preserve all current row bindings and add one row")
                location = dict(insertion_location or {})
                index = location.get("index")
                if (location.get("field") != "instructions" or index != len(current_instructions)):
                    raise ReviewConflict("stale insertion must be explicitly placed at the current end of instructions")
                confirmed_location = {"field": "instructions", "index": index}
                expected_recipe = json.loads(_canonical(current_recipe))
                expected_recipe["instructions"].append(value)
                submitted_recipe = json.loads(_canonical(recipe))
                submitted_recipe["suggestions"] = expected_recipe.get("suggestions", [])
                if _canonical(submitted_recipe) != _canonical(expected_recipe):
                    fields = sorted(key for key in set(submitted_recipe) | set(expected_recipe)
                                    if _canonical(submitted_recipe.get(key)) != _canonical(expected_recipe.get(key)))
                    raise ReviewConflict("rebased recipe must preserve current content; changed fields: "
                                         + ", ".join(fields))
                recipe = expected_recipe
                digest = _recipe_digest(recipe)
                inserted_rows = [item for item in provided_rows
                                 if row_identity(item) not in current_identities]
                inserted_target = (inserted_rows[0] if len(inserted_rows) == 1 else None)
                all_current_target_ids = {str(item.get("target_id", "")) for item in current_targets}
                if (not inserted_target or inserted_target.get("field") != "instructions"
                        or inserted_target.get("position") != index
                        or str(inserted_target.get("target_id", "")) in all_current_target_ids):
                    raise ReviewConflict("rebased insertion needs a target binding at the current end")
                canonical_targets.append({
                    "target_id": str(inserted_target["target_id"]), "target_kind": "row",
                    "field": "instructions", "position": index,
                    "value_sha256": hashlib.sha256(value.encode("utf-8")).hexdigest(),
                })

            revision_event = self._base_event("review_revision", candidate_id,
                                              proposal["source_hash"], proposal["extraction_id"],
                                              f"{operation_id}:revision")
            revision_event.update({"revision": expected_parent + 1, "parent_revision": expected_parent,
                                   "recipe": recipe, "target_bindings": canonical_targets,
                                   "raw_bulk_text": {"ingredients": "", "instructions": ""},
                                   "mode": "structured", "warnings": [],
                                   "content_sha256": digest, "content_digest_version": 1})
            decision_event = self._base_event("dynamic_suggestion_decision", candidate_id,
                                              proposal["source_hash"], proposal["extraction_id"],
                                              f"{operation_id}:decision")
            decision_event.update({"proposal_id": proposal_id, "request_id": proposal["request_id"],
                                   "base_revision": proposal["revision"],
                                   "revision": expected_parent + 1,
                                   "application_revision": expected_parent + 1,
                                   "application_content_digest": digest,
                                   "content_digest": proposal["content_digest"],
                                   "disposition": disposition, "final_value": value,
                                   "confirmed_rebase": True,
                                   "rebase_from_revision": proposal["revision"],
                                   "rebase_from_digest": proposal["content_digest"],
                                   "rebase_to_revision": expected_parent,
                                   "rebase_to_digest": expected_content_digest,
                                   "target_id": target_id,
                                   "comparison_current_value": comparison_current_value,
                                   "comparison_insertion_location": confirmed_location})
            expected_revision = {key: value for key, value in revision_event.items()
                                 if key not in {"event_id", "timestamp", "actor"}}
            expected_decision = {key: value for key, value in decision_event.items()
                                 if key not in {"event_id", "timestamp", "actor"}}
            prior_revision = self._existing_operation(f"{operation_id}:revision", "review_revision", expected_revision)
            prior_decision = self._existing_operation(f"{operation_id}:decision", "dynamic_suggestion_decision", expected_decision)
            if prior_revision or prior_decision:
                if prior_revision and prior_decision:
                    return prior_revision, prior_decision
                raise ReviewConflict("rebase operation has incomplete durable history")
            self._append_many([revision_event, decision_event], locked=True)
            self._reload()
            return self._readback(revision_event), self._readback(decision_event)

    def apply_dynamic_suggestion(self, *, candidate_id: str, proposal_id: str,
                                 expected_parent: int, recipe: dict,
                                 target_bindings: list[dict], disposition: str,
                                 final_value: str | None, operation_id: str) -> tuple[dict, dict]:
        """Atomically save accepted wording and its proposal decision at one revision."""
        if disposition not in {"accepted", "edited"}:
            raise ValueError("application requires accepted or edited disposition")
        value = str(final_value or "").strip()
        if not value:
            raise ValueError("accepted suggestion content cannot be empty")
        digest = _recipe_digest(recipe)
        with _state_lock(self.path):
            self._reload()
            proposal = next((item for item in self.events if item.get("event") == "dynamic_suggestion"
                             and item.get("candidate_id") == candidate_id
                             and item.get("proposal_id") == proposal_id), None)
            if not proposal or proposal.get("status") != "pending":
                raise ReviewConflict("generated proposal is unavailable or already resolved")
            revisions = [item for item in self.revisions.get(candidate_id, [])
                         if item.get("source_hash") == proposal["source_hash"]
                         and item.get("extraction_id") == proposal["extraction_id"]]
            latest = max(revisions, key=lambda item: int(item["revision"]), default=None)
            if (latest is None or int(latest["revision"]) != int(expected_parent)
                    or int(latest["revision"]) != int(proposal["revision"])
                    or latest.get("content_sha256") != proposal.get("content_digest")):
                raise ReviewConflict("recipe changed; proposal is stale and requires an explicit rebase decision")
            location = proposal.get("insertion_location") or {}
            values = recipe.get("instructions")
            target = next((item for item in target_bindings
                           if item.get("target_id") == proposal.get("target_id")
                           and item.get("field") == "instructions"), None)
            position = target.get("position") if target else None
            if not isinstance(values, list) or value not in values:
                raise ReviewConflict("accepted wording must be present in the saved instruction revision")
            if proposal.get("target_id") and not (isinstance(position, int) and 0 <= position < len(values)):
                raise ReviewConflict("proposal target is missing in the saved revision")
            if not proposal.get("target_id") and location.get("field") != "instructions":
                raise ReviewConflict("proposal insertion location is not bound to instructions")
            revision_event = self._base_event("review_revision", candidate_id,
                                              proposal["source_hash"], proposal["extraction_id"],
                                              f"{operation_id}:revision")
            revision_event.update({"revision": expected_parent + 1, "parent_revision": expected_parent,
                                   "recipe": recipe, "target_bindings": target_bindings,
                                   "raw_bulk_text": {"ingredients": "", "instructions": ""},
                                   "mode": "structured", "warnings": [],
                                   "content_sha256": digest, "content_digest_version": 1})
            decision_event = self._base_event("dynamic_suggestion_decision", candidate_id,
                                              proposal["source_hash"], proposal["extraction_id"],
                                              f"{operation_id}:decision")
            decision_event.update({"proposal_id": proposal_id, "request_id": proposal["request_id"],
                                   "base_revision": expected_parent, "revision": expected_parent + 1,
                                   "application_revision": expected_parent + 1,
                                   "application_content_digest": digest, "content_digest": proposal["content_digest"],
                                   "disposition": disposition, "final_value": value})
            if any(item.get("proposal_id") == proposal_id
                   and item.get("event") == "dynamic_suggestion_decision" for item in self.events):
                raise ReviewConflict("proposal already has a recorded decision")
            current = max((int(item["revision"]) for item in revisions), default=0)
            if current != expected_parent:
                raise ReviewConflict(f"revision changed: expected {expected_parent}, current {current}")
            self._append_many([revision_event, decision_event], locked=True)
            self._reload()
            return self._readback(revision_event), self._readback(decision_event)

    def record_no_instructions_decision(self, *, candidate_id: str, source_hash: str,
                                       extraction_id: str, revision: int, reason: str,
                                       operation_id: str) -> dict:
        """Record an explicit no-directions decision for one immutable revision."""
        reason = str(reason or "").strip()
        if not reason:
            raise ReviewConflict("a reason is required to confirm that no instructions are needed")
        with _state_lock(self.path):
            self._reload()
            registration, identity_status, _ = self._effective_identity_authority(
                candidate_id, source_hash, extraction_id)
            snapshot = next((item for item in self.revisions.get(candidate_id, [])
                             if int(item.get("revision", -1)) == revision
                             and item.get("source_hash") == source_hash
                             and item.get("extraction_id") == extraction_id), None)
            if not registration:
                raise ReviewConflict("candidate must be registered before recording this decision")
            if identity_status != "registered":
                raise ReviewConflict("candidate identity requires explicit reconciliation")
            if not snapshot or snapshot.get("content_digest_version") != 1:
                raise ReviewConflict("the exact review revision is not present")
            if _recipe_digest(snapshot.get("recipe", {})) != snapshot.get("content_sha256"):
                raise ReviewConflict("review revision content digest does not match its recipe snapshot")
            recipe = RecipeCandidate.model_validate(snapshot["recipe"])
            if not recipe.name.strip() or not any(str(item).strip() for item in recipe.ingredients):
                raise ReviewConflict("recipe title and ingredients are required before this decision")
            if any(str(item).strip() for item in recipe.instructions):
                raise ReviewConflict("remove the instructions in the saved revision before recording this decision")
            event = self._base_event("review_no_instructions_decision", candidate_id,
                                     source_hash, extraction_id, operation_id)
            event.update({"authority_version": 1, "revision": revision,
                          "content_sha256": snapshot["content_sha256"], "reason": reason})
            expected = {key: value for key, value in event.items()
                        if key not in {"event_id", "timestamp", "actor"}}
            prior = self._existing_operation(operation_id, "review_no_instructions_decision", expected)
            if prior:
                return prior
            current = max((int(item["revision"]) for item in self.revisions.get(candidate_id, [])
                           if item.get("source_hash") == source_hash
                           and item.get("extraction_id") == extraction_id), default=0)
            if revision != current:
                raise ReviewConflict(f"revision changed: expected {revision}, current {current}")
            self._append(event, locked=True)
            self._reload()
            return self._readback(event)

    def record_source_relink(self, *, candidate_id: str, source_hash: str,
                             extraction_id: str, source_path: str | Path,
                             operation_id: str) -> dict:
        """Record a recovered location only after its bytes match the original digest."""
        path = Path(source_path)
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError as exc:
            raise ReviewConflict("recovered source file is unavailable") from exc
        if digest != source_hash:
            raise ReviewConflict("recovered source bytes do not match the registered source hash")
        with _state_lock(self.path):
            self._reload()
            registration, identity_status, _ = self._effective_identity_authority(
                candidate_id, source_hash, extraction_id)
            if not registration:
                raise ReviewConflict("candidate registration is unavailable")
            if identity_status != "registered":
                raise ReviewConflict("candidate identity requires explicit reconciliation")
            event = self._base_event("review_source_relink", candidate_id, source_hash,
                                     extraction_id, operation_id)
            event.update({"authority_version": 1, "source_path": str(path.resolve()),
                          "source_sha256": digest})
            expected = {key: value for key, value in event.items()
                        if key not in {"event_id", "timestamp", "actor"}}
            prior = self._existing_operation(operation_id, "review_source_relink", expected)
            if prior:
                return prior
            self._append(event, locked=True)
            self._reload()
            return self._readback(event)

    def approve_revision(self, *, candidate_id: str, source_hash: str, extraction_id: str,
                         revision: int, operation_id: str, source_path: str | Path,
                         approval_method: str = "source_verified",
                         reviewer_acknowledged: bool = False,
                         review_rationale: str = "") -> dict:
        with _state_lock(self.path):
            self._reload()
            registration, identity_status, _ = self._effective_identity_authority(
                candidate_id, source_hash, extraction_id)
            if not registration:
                raise ReviewConflict("candidate must be registered before approval")
            snapshot = next((item for item in self.revisions.get(candidate_id, [])
                             if int(item["revision"]) == revision
                             and item["source_hash"] == source_hash
                             and item["extraction_id"] == extraction_id), None)
            if not snapshot:
                raise ReviewConflict("the exact review revision is not present")
            recipe = RecipeCandidate.model_validate(snapshot["recipe"])
            no_instructions = next((item for item in reversed(
                self.no_instructions_decisions.get(candidate_id, []))
                if item.get("source_hash") == source_hash
                and item.get("extraction_id") == extraction_id
                and item.get("revision") == revision
                and item.get("content_sha256") == snapshot.get("content_sha256")
                and item.get("authority_version") == 1), None)
            if not is_complete(recipe, no_instructions_confirmed=bool(no_instructions)):
                raise ReviewConflict("an empty or incomplete recipe cannot be approved")
            if approval_method not in {"source_verified", "manual_source_unavailable"}:
                raise ReviewConflict("approval method is not supported")
            review_rationale = str(review_rationale or "").strip()
            if approval_method == "manual_source_unavailable" and not reviewer_acknowledged:
                raise ReviewConflict("reviewer acknowledgment is required for manual approval")
            if approval_method == "manual_source_unavailable" and not review_rationale:
                raise ReviewConflict("a review rationale is required for manual approval")
            if (snapshot.get("content_digest_version") != 1
                    or _recipe_digest(snapshot["recipe"]) != snapshot.get("content_sha256")):
                raise ReviewConflict("review revision content digest does not match its recipe snapshot")
            if identity_status != "registered":
                raise ReviewConflict("candidate identity requires explicit reconciliation")
            registration, identity_status, protected = self._effective_identity_authority(
                candidate_id, source_hash, extraction_id, registration=registration,
                check_import_protection=True)
            if not registration or identity_status != "registered":
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
            dynamic = [item for item in self._dynamic_suggestions_by_candidate.get(candidate_id, [])
                       if item.get("source_hash") == source_hash
                       and item.get("extraction_id") == extraction_id]
            decisions = {item.get("proposal_id"): item
                         for item in self._dynamic_decisions_by_candidate.get(candidate_id, {}).values()
                         if item.get("revision") == revision
                         and item.get("disposition") in {"accepted", "edited", "declined", "stale_discarded"}}
            if any(item.get("status") in {"pending", "stale", "target_missing"}
                   and item.get("proposal_id") not in decisions
                   for item in dynamic):
                raise ReviewConflict("pending generated proposals must be resolved before approval")
            active_requests = [item for item in self._suggestion_requests_by_candidate.get(candidate_id, [])
                               if item.get("source_hash") == source_hash
                               and item.get("extraction_id") == extraction_id]
            if any(self._suggestion_request_status_loaded(item["request_id"]) in {"queued", "running"}
                   for item in active_requests):
                raise ReviewConflict("a generated suggestion request is still in progress")
            event = self._base_event("review_approval", candidate_id, source_hash,
                                     extraction_id, operation_id)
            event.update({"authority_version": 1, "approval_method": approval_method,
                          "reviewer_acknowledged": bool(reviewer_acknowledged),
                          "reviewer_acknowledgment": (
                              MANUAL_APPROVAL_ACKNOWLEDGMENT
                              if approval_method == "manual_source_unavailable" and reviewer_acknowledged else ""),
                          "review_rationale": review_rationale,
                          "no_instructions_reason": no_instructions.get("reason", "") if no_instructions else "",
                          "revision": revision, "content_sha256": snapshot["content_sha256"],
                          "content_digest_version": snapshot.get("content_digest_version", 1),
                          "source_path": str(source_path),
                          "resolution_event_ids": [item["event_id"] for item in resolutions],
                          "resolution_sha256": _resolution_digest(resolutions)})
            expected = {k: v for k, v in event.items()
                        if k not in {"event_id", "timestamp", "actor"}}
            prior = self._existing_operation(operation_id, "review_approval", expected)
            if prior:
                return prior
            if protected:
                raise ReviewConflict("candidate has a protected import outcome and cannot receive new approval")
            try:
                actual_source_hash = hashlib.sha256(Path(source_path).read_bytes()).hexdigest()
            except OSError as exc:
                if approval_method != "manual_source_unavailable":
                    raise ReviewConflict("source scan is unavailable; no new approval was recorded") from exc
            else:
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
        registration, identity_status, _ = self._effective_identity_authority(
            candidate_id, source_hash, extraction_id)
        current_resolutions = [item for item in self.resolutions.get(candidate_id, [])
                               if item.get("source_hash") == source_hash
                               and item.get("extraction_id") == extraction_id
                               and item.get("revision") == latest["revision"]]
        required_resolution_ids = {item["event_id"] for item in current_resolutions}
        resolution_sha256 = _resolution_digest(current_resolutions)
        approvals = [item for item in self.approvals.get(candidate_id, [])
                     if item.get("revision") == latest["revision"]
                     and item.get("source_hash") == source_hash
                     and item.get("content_sha256") == latest["content_sha256"]
                     and item.get("extraction_id") == extraction_id
                     and required_resolution_ids == set(item.get("resolution_event_ids", []))
                     and item.get("resolution_sha256") == resolution_sha256]
        approval = max(approvals, key=lambda item: item.get("timestamp", ""), default=None)
        no_instructions = next((item for item in reversed(
            self.no_instructions_decisions.get(candidate_id, []))
            if item.get("source_hash") == source_hash
            and item.get("extraction_id") == extraction_id
            and item.get("revision") == latest["revision"]
            and item.get("content_sha256") == latest.get("content_sha256")
            and item.get("authority_version") == 1 and str(item.get("reason", "")).strip()), None)
        approval_method = str((approval or {}).get("approval_method", "source_verified"))
        authority_valid = bool(approval and (
            approval_method == "source_verified"
            or (approval_method == "manual_source_unavailable"
                and approval.get("authority_version") == 1
                and approval.get("reviewer_acknowledged") is True
                and approval.get("reviewer_acknowledgment") == MANUAL_APPROVAL_ACKNOWLEDGMENT
                and str(approval.get("review_rationale", "")).strip())
        ) and (not no_instructions
               or approval.get("no_instructions_reason") == no_instructions.get("reason")))
        snapshot_integrity_valid = (
            latest.get("content_digest_version") == 1
            and _recipe_digest(latest.get("recipe", {})) == latest.get("content_sha256")
        )
        legacy_draft_conflict = bool(
            approval and legacy_draft and legacy_draft.saved_at
            and legacy_draft.saved_at > approval.get("timestamp", "")
        )
        approved = bool(authority_valid and snapshot_integrity_valid and not legacy_draft_conflict and registration
                        and identity_status == "registered")
        recipe = latest["recipe"]
        recipe = json.loads(json.dumps(recipe, ensure_ascii=False))
        try:
            complete = is_complete(RecipeCandidate.model_validate(recipe),
                                   no_instructions_confirmed=bool(no_instructions))
        except (TypeError, ValueError):
            complete = False
        resolutions = tuple(item for item in self.resolutions.get(candidate_id, [])
                            if item.get("extraction_id") == extraction_id
                            and item.get("revision") == latest["revision"])
        dynamic = [item for item in self._dynamic_suggestions_by_candidate.get(candidate_id, [])
                   if item.get("source_hash") == source_hash
                   and item.get("extraction_id") == extraction_id]
        dynamic_decisions = self._dynamic_decisions_by_candidate.get(candidate_id, {})
        resolved_dynamic_ids = {key for key, item in dynamic_decisions.items()
                                if item.get("disposition") in {"accepted", "edited", "declined", "stale_discarded"}}
        for item in dynamic:
            decision = dynamic_decisions.get(item.get("proposal_id"))
            proposal_is_current = (int(item.get("revision", -1)) == int(latest["revision"])
                                    and item.get("content_digest") == latest.get("content_sha256"))
            status = (decision.get("disposition") if decision else item.get("status", "pending"))
            final_value = (decision.get("final_value") or item.get("proposed_value")) if decision else ""
            applied_in_later_revision = False
            if decision and decision.get("application_revision"):
                applied_revision = next((revision for revision in self.revisions.get(candidate_id, [])
                                         if int(revision.get("revision", -1)) == int(decision["application_revision"])
                                         and revision.get("content_sha256") == decision.get("application_content_digest")), None)
                applied_in_later_revision = applied_revision is not None
            has_durable_decision = bool(decision and (
                decision.get("disposition") in {"declined", "rejected", "stale_discarded"}
                or applied_in_later_revision
            ))
            if not proposal_is_current and not has_durable_decision:
                status = "stale"
            review_status = {"accepted": "approved", "edited": "edited",
                             "declined": "declined", "rejected": "rejected",
                             "stale_discarded": "rejected"}.get(status, "pending")
            projected_suggestion = {
                "suggestion_id": item["proposal_id"], "field_path": item["field_path"],
                "proposed_value": item["proposed_value"], "rationale": item["rationale"],
                "confidence": "low", "uncertainty": item["uncertainty"],
                "review_status": review_status, "generated": True,
                "classification": item["classification"], "request_id": item["request_id"],
                "target_id": item.get("target_id", ""),
                "insertion_location": item.get("insertion_location") or {},
                "dynamic_status": status,
            }
            suggestions = recipe.setdefault("suggestions", [])
            existing_suggestion = next((value for value in suggestions
                                        if value.get("suggestion_id") == item["proposal_id"]), None)
            if existing_suggestion is None:
                suggestions.append(projected_suggestion)
            else:
                existing_suggestion.update(projected_suggestion)
            if proposal_is_current and decision and decision.get("disposition") in {"accepted", "edited"}:
                value = decision.get("final_value") or item["proposed_value"]
                location = item.get("insertion_location") or {}
                field_name = str(location.get("field", ""))
                values = recipe.get(field_name)
                if field_name == "instructions" and isinstance(values, list):
                    index = location.get("index")
                    if index is None and location.get("after") is None and not values:
                        values.append(value)
                    elif isinstance(index, int) and 0 <= index <= len(values):
                        values.insert(index, value)
                    elif item.get("target_id"):
                        target = next((target for target in latest.get("target_bindings", [])
                                       if target.get("target_id") == item["target_id"]
                                       and target.get("field") == "instructions"), None)
                        position = target.get("position") if target else None
                        if isinstance(position, int) and 0 <= position < len(values):
                            values[position] = value
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
                                "rejected": "rejected", "not_recipe_content": "declined",
                            }[disposition]
            resolved_findings = {item.get("issue_id") for item in registration["target_bindings"]
                                 if item.get("target_kind") == "finding"
                                 and any(res.get("issue_id") == item.get("issue_id")
                                         and res.get("disposition") in {"source_corrected", "human_completion", "not_recipe_content"}
                                         for res in resolutions)}
            recipe["missing_information"] = [item for item in recipe.get("missing_information", [])
                                              if not any(issue_id.endswith(":" + item.get("field_path", ""))
                                                         for issue_id in resolved_findings)]
        try:
            complete = is_complete(RecipeCandidate.model_validate(recipe),
                                   no_instructions_confirmed=bool(no_instructions))
        except (TypeError, ValueError):
            complete = False
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
        pending = pending or any(
            item.get("proposal_id") not in resolved_dynamic_ids
            and (item.get("status") in {"stale", "target_missing"}
                 or (item.get("status") == "pending"
                     and int(item.get("revision", -1)) == int(latest["revision"])))
            for item in dynamic
        )
        active_request = any(
            self._suggestion_request_status_loaded(item["request_id"]) in {"queued", "running"}
            for item in self._suggestion_requests_by_candidate.get(candidate_id, [])
            if item.get("source_hash") == source_hash and item.get("extraction_id") == extraction_id)
        approved = approved and not pending and not active_request
        if any(dynamic_decisions.get(item.get("proposal_id"), {}).get("disposition") in {"accepted", "edited"}
               and int(item.get("revision", -1)) == int(latest["revision"])
               for item in dynamic):
            approved = False
        unmapped_resolution = bool(registration and any(
            resolution.get("disposition") in {"source_corrected", "human_completion", "accepted", "edited"}
            and not _mapped_issue_target(issue, latest)
            for resolution in resolutions
            for issue in registration["target_bindings"]
            if issue.get("issue_id") == resolution.get("issue_id")))
        import_ready = approved and complete and not pending and not active_request and not unmapped_resolution
        if import_ready:
            recipe["requires_review"] = False
        return EffectiveReview(candidate_id, recipe, int(latest["revision"]),
                               latest["content_sha256"], approved,
                               import_ready, resolutions, legacy_draft_conflict,
                               tuple(latest.get("target_bindings", [])),
                               approval_method if authority_valid else "",
                               str((approval or {}).get("review_rationale", "")) if authority_valid else "",
                               str(no_instructions.get("reason", "")) if no_instructions else "")

    def add_candidate(self, source: str, candidate_id: str, candidate: RecipeCandidate) -> None:
        if candidate_id in self.candidates:
            return
        event = {
            "event": "candidate",
            "source": source,
            "candidate_id": candidate_id,
            "candidate": _candidate_payload(candidate),
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
                        "rejected": "rejected", "not_recipe_content": "declined",
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
                    effective = self.project_loaded(
                        identity, str(registration.get("source_hash", "")),
                        str(registration.get("extraction_id", "")), candidate,
                    )
                    candidate = RecipeCandidate.model_validate(effective.recipe)
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
                for finding in validate_recipe(candidate):
                    writer.writerow({
                        "source": event["source"], "candidate_id": identity,
                        "recipe": candidate.name, "suggestion_id": finding.diagnostic_id,
                        "field": finding.field_path, "diagnosis": finding.category,
                        "detail": finding.explanation, "source_evidence": finding.original_text,
                        "suggested_completion": "", "confidence": "",
                        "uncertainty": "Heuristic finding; verify against the source.",
                        "review_status": "pending", "approved_value": "",
                        "suggested_action": finding.suggested_action,
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
        if "notes" in values:
            value = values["notes"]
            if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                raise ValueError("Edited candidate notes must be a list of strings")
            updated.notes = value
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
    if field_path == "notes":
        candidate.notes = _split_list_value(value)
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
