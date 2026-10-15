"""Append-only local review queue for extraction diagnostics and proposals."""
from __future__ import annotations

import csv
import json
import os
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from recipe_quality import RecipeCandidate, Suggestion


@dataclass(frozen=True)
class ReviewDecision:
    suggestion_id: str
    status: str
    final_value: Optional[str]


class ReviewStore:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
        self.candidates: dict[str, dict] = {}
        self.decisions: dict[str, list[ReviewDecision]] = {}
        self.candidate_decisions: dict[str, list[ReviewDecision]] = {}
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                event = json.loads(line)
                identity = event.get("candidate_id", "")
                if event.get("event") == "candidate":
                    self.candidates[identity] = event
                elif event.get("event") == "decision":
                    self.decisions.setdefault(identity, []).append(ReviewDecision(
                        event["suggestion_id"], event["status"], event.get("final_value")
                    ))
                elif event.get("event") == "candidate_decision":
                    self.candidate_decisions.setdefault(identity, []).append(ReviewDecision(
                        "", event["status"], event.get("final_value")
                    ))

    def _append(self, event: dict) -> None:
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, ensure_ascii=False) + "\n")
                fh.flush()
                os.fsync(fh.fileno())

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
        event = {
            "event": "candidate_decision", "source": source,
            "candidate_id": candidate_id, "status": status,
            "final_value": final_value,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        self._append(event)
        self.candidate_decisions.setdefault(candidate_id, []).append(
            ReviewDecision("", status, final_value)
        )

    def record(self, source: str, candidate_id: str, candidate: RecipeCandidate,
               suggestion_id: str, status: str, final_value: Optional[str] = None) -> None:
        if status not in {"pending", "approved", "edited", "rejected"}:
            raise ValueError(f"Unsupported review status: {status}")
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
            ReviewDecision(suggestion_id, status, final_value)
        )

    def decisions_for(self, candidate_id: str) -> list[ReviewDecision]:
        return list(self.decisions.get(candidate_id, []))

    def candidate_decision(self, candidate_id: str) -> Optional[ReviewDecision]:
        decisions = self.candidate_decisions.get(candidate_id, [])
        return decisions[-1] if decisions else None

    def write_csv(self, path: Path) -> None:
        fields = ["source", "candidate_id", "recipe", "suggestion_id", "field", "diagnosis",
                  "detail", "source_evidence", "suggested_completion", "confidence",
                  "uncertainty", "review_status", "approved_value", "suggested_action"]
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="", encoding="utf-8-sig") as fh:
            writer = csv.DictWriter(fh, fieldnames=fields)
            writer.writeheader()
            for identity, event in self.candidates.items():
                candidate = RecipeCandidate.model_validate(event["candidate"])
                decisions = {d.suggestion_id: d for d in self.decisions_for(identity)}
                suggestions = {s.field_path: s for s in candidate.suggestions}
                written_fields = set()
                for finding in candidate.missing_information:
                    written_fields.add(finding.field_path)
                    suggestion = suggestions.get(finding.field_path)
                    decision = decisions.get(suggestion.suggestion_id) if suggestion else None
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
                        "approved_value": decision.final_value if decision and decision.status in {"approved", "edited"} else "",
                        "suggested_action": finding.suggested_action,
                    })
                for suggestion in candidate.suggestions:
                    if suggestion.field_path in written_fields:
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
