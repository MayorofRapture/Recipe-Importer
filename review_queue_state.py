"""Durable navigation history kept separate from recipe review authority."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from review_store import _state_lock


class QueueState:
    """Append-only local skip and return history for the review workspace."""

    def __init__(self, output_dir: Path | str):
        self.path = Path(output_dir) / "review_queue_events.jsonl"

    def _read(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        events: list[dict[str, Any]] = []
        with self.path.open("r", encoding="utf-8") as handle:
            for number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"Review navigation history is unreadable at line {number}.") from exc
                if not isinstance(event, dict) or event.get("event") not in {"visit", "skip", "back"}:
                    raise ValueError(f"Review navigation history has an invalid record at line {number}.")
                events.append(event)
        return events

    def _append(self, event: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with _state_lock(self.path):
            events = self._read()
            if event["event"] == "visit":
                latest_back = next((item for item in reversed(events) if item.get("event") == "back"), None)
                latest_visit_index = max((index for index, item in enumerate(events)
                                          if item.get("event") == "visit"), default=-1)
                if (latest_back and events.index(latest_back) > latest_visit_index
                        and latest_back.get("candidate_id") == event["candidate_id"]):
                        return
                if latest_back and events.index(latest_back) > latest_visit_index:
                    with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                        handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
                        handle.flush()
                        os.fsync(handle.fileno())
                    return
                prior = next((item for item in reversed(events) if item.get("event") == "visit"), None)
                if prior and prior.get("candidate_id") == event["candidate_id"]:
                    return
            with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())

    @staticmethod
    def _record(kind: str, candidate_id: str, title: str, context: dict[str, Any]) -> dict[str, Any]:
        return {"event": kind, "candidate_id": str(candidate_id), "title": str(title),
                "context": dict(context)}

    def record_visit(self, candidate_id: str, title: str, context: dict[str, Any]) -> None:
        self._append(self._record("visit", candidate_id, title, context))

    def record_skip(self, candidate_id: str, title: str, context: dict[str, Any]) -> None:
        self._append(self._record("skip", candidate_id, title, context))

    def back(self, current_id: str | None, *, record: bool = False) -> dict[str, Any] | None:
        events = self._read()
        visits = [event for event in events if event["event"] == "visit"]
        if not visits:
            return None
        last_visit_event_index = max((index for index, item in enumerate(events)
                                      if item.get("event") == "visit"), default=-1)
        latest_back = next((item for item in reversed(events) if item.get("event") == "back"), None)
        cursor = (int(latest_back.get("cursor", len(visits) - 1))
                  if latest_back and events.index(latest_back) > last_visit_event_index
                  else len(visits) - 1)
        while cursor >= 0 and visits[cursor]["candidate_id"] == current_id:
            cursor -= 1
        if cursor < 0:
            return None
        target = visits[cursor]
        if record:
            self._append({"event": "back", "candidate_id": target["candidate_id"],
                          "title": target["title"], "context": target.get("context", {}),
                          "cursor": cursor})
        return target

    def skipped(self) -> list[dict[str, Any]]:
        latest: dict[str, dict[str, Any]] = {}
        for event in self._read():
            if event["event"] == "skip":
                latest[event["candidate_id"]] = event
        return list(latest.values())

    def is_skipped(self, candidate_id: str) -> bool:
        return any(event["candidate_id"] == candidate_id for event in self.skipped())
