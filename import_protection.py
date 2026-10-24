"""Fail-closed projection of local Mealie creation protection."""
from __future__ import annotations

import json
from pathlib import Path
from ui_diagnostics import count as ui_count


class ImportProtectionError(RuntimeError):
    """Local import history cannot safely prove a candidate is unprotected."""


_PROTECTED_STATE = {"IMPORTED", "UNCERTAIN", "NEEDS_MANUAL_CHECK"}


def protected_candidate_ids(output_dir: Path, state_path: Path | None = None) -> set[str]:
    output_dir = Path(output_dir)
    state_path = Path(state_path) if state_path is not None else output_dir / "import_state.jsonl"
    event_path = output_dir / "ui_import_events.jsonl"
    state_records = read_import_state(state_path)
    event_records = _read_jsonl(event_path)
    return protected_candidate_ids_from_records(state_records, event_records)


def read_import_state(state_path: Path) -> list[dict]:
    """Read raw state once under the caller's synchronization boundary."""
    try:
        records = _read_jsonl(Path(state_path))
    except ImportProtectionError:
        raise
    if any(not isinstance(record, dict) for record in records):
        raise ImportProtectionError("import history is unreadable; candidates remain protected")
    return records


def protected_candidate_ids_from_records(state_records: list[dict],
                                         event_records: list[dict]) -> set[str]:
    protected: set[str] = set()
    for record in state_records:
        if record.get("status") in _PROTECTED_STATE:
            protected.update(str(record.get(key, "")) for key in ("identity", "key")
                             if record.get(key))
    attempts: dict[str, tuple[str, str]] = {}
    terminal: dict[str, str] = {}
    for event in event_records:
        attempt_id = str(event.get("attempt_id", ""))
        candidate_id = str(event.get("candidate_id", ""))
        kind = event.get("event")
        if kind == "intent_started" and attempt_id and candidate_id:
            attempts[attempt_id] = (candidate_id, "started")
        elif kind in {"create_rejected", "create_uncertain", "creation_confirmed", "reconciled_existing"} and attempt_id:
            terminal[attempt_id] = str(kind)
            if candidate_id and kind != "create_rejected":
                protected.add(candidate_id)
    for attempt_id, (candidate_id, _) in attempts.items():
        if terminal.get(attempt_id) != "create_rejected":
            protected.add(candidate_id)
    # A completed create may be represented only by a non-attempt legacy audit event.
    for event in event_records:
        if event.get("event") in {"creation_confirmed", "reconciled_existing"}:
            if event.get("candidate_id"):
                protected.add(str(event["candidate_id"]))
    protected.discard("")
    return protected


def protected_identity_groups(review, protected_ids: set[str]) -> set[str]:
    """Project protected IDs through only ReviewStore-validated identity links."""
    alias_groups: dict[str, set[str]] = {}
    valid_links = review._identity_link_group_index
    for candidate_id, registrations in review.registrations.items():
        for registration in registrations:
            if registration.get("event") != "candidate_registration":
                continue
            source_hash = str(registration.get("source_hash", ""))
            extraction_id = str(registration.get("extraction_id", ""))
            key = (candidate_id, source_hash, extraction_id)
            if registration.get("identity_status") == "registered":
                group_id = str(registration.get("identity_group_id") or candidate_id)
            else:
                group_id = str(valid_links.get(key, ""))
            if not group_id:
                continue
            aliases = {candidate_id, str(registration.get("legacy_id", "")),
                       str(registration.get("title_legacy_id", "")),
                       *(str(value) for value in registration.get("legacy_aliases", ())) }
            for alias in aliases - {""}:
                alias_groups.setdefault(alias, set()).add(group_id)
    return {group_id for identity in protected_ids
            for group_id in alias_groups.get(identity, ())}


def candidate_identity_group(review, candidate_id: str, source_hash: str,
                             extraction_id: str) -> str:
    registrations = [item for item in review.registrations.get(candidate_id, ())
                     if item.get("event") == "candidate_registration"
                     and item.get("source_hash") == source_hash
                     and item.get("extraction_id") == extraction_id]
    if len(registrations) != 1:
        return ""
    registration = registrations[0]
    if registration.get("identity_status") == "registered":
        return str(registration.get("identity_group_id") or candidate_id)
    return str(review._identity_link_group_index.get((candidate_id, source_hash, extraction_id), ""))


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        ui_count("ledger_reads")
        records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
                   if line.strip()]
    except (OSError, json.JSONDecodeError) as exc:
        raise ImportProtectionError("import history is unreadable; candidates remain protected") from exc
    if any(not isinstance(record, dict) for record in records):
        raise ImportProtectionError("import history is unreadable; candidates remain protected")
    return records
