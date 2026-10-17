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
    protected: set[str] = set()
    for path in (state_path, event_path):
        if not path.exists():
            continue
        try:
            ui_count("ledger_reads")
            records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
                       if line.strip()]
        except (OSError, json.JSONDecodeError) as exc:
            raise ImportProtectionError("import history is unreadable; candidates remain protected") from exc
        for record in records:
            if path == state_path and record.get("status") in _PROTECTED_STATE:
                protected.update(str(record.get(key, "")) for key in ("identity", "key")
                                 if record.get(key))

    if not event_path.exists():
        return protected
    attempts: dict[str, tuple[str, str]] = {}
    terminal: dict[str, str] = {}
    for event in records:
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
    for event in records:
        if event.get("event") in {"creation_confirmed", "reconciled_existing"}:
            if event.get("candidate_id"):
                protected.add(str(event["candidate_id"]))
    protected.discard("")
    return protected
