"""Separate append-only durable draft storage for the Streamlit review UI."""
from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class DraftRecord:
    draft_id: str
    candidate_id: str
    source_hash: str
    source_path: str
    recipe: dict[str, Any]
    raw_ingredients: str
    raw_instructions: str
    mode: str
    warnings: list[str]
    revision: int
    saved_at: str

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "DraftRecord":
        return cls(
            draft_id=str(value["draft_id"]), candidate_id=str(value["candidate_id"]),
            source_hash=str(value.get("source_hash", "")),
            source_path=str(value.get("source_path", "")),
            recipe=dict(value.get("recipe") or {}),
            raw_ingredients=str(value.get("raw_ingredients", "")),
            raw_instructions=str(value.get("raw_instructions", "")),
            mode=str(value.get("mode", "quick")),
            warnings=[str(item) for item in value.get("warnings", [])],
            revision=int(value.get("revision", 1)),
            saved_at=str(value.get("saved_at", "")),
        )


class DraftStore:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()

    def save(
        self,
        candidate_id: str,
        source_hash: str,
        source_path: str,
        recipe: dict[str, Any],
        raw_ingredients: str,
        raw_instructions: str,
        mode: str,
        warnings: list[str],
    ) -> DraftRecord:
        if not candidate_id.strip():
            raise ValueError("candidate_id must not be empty")
        with self._lock:
            previous = self.latest(candidate_id)
            record = DraftRecord(
                draft_id=f"{candidate_id}:{(previous.revision if previous else 0) + 1}",
                candidate_id=candidate_id,
                source_hash=source_hash,
                source_path=source_path,
                recipe=json.loads(json.dumps(recipe, ensure_ascii=False)),
                raw_ingredients=raw_ingredients,
                raw_instructions=raw_instructions,
                mode=mode,
                warnings=list(warnings),
                revision=(previous.revision if previous else 0) + 1,
                saved_at=datetime.now(timezone.utc).isoformat(),
            )
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(asdict(record), ensure_ascii=False) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            return record

    def latest(self, candidate_id: str) -> DraftRecord | None:
        latest: DraftRecord | None = None
        if not self.path.exists():
            return None
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            value = DraftRecord.from_dict(json.loads(line))
            if value.candidate_id == candidate_id:
                latest = value
        return latest

    def all_latest(self) -> dict[str, DraftRecord]:
        records: dict[str, DraftRecord] = {}
        if not self.path.exists():
            return records
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                record = DraftRecord.from_dict(json.loads(line))
                records[record.candidate_id] = record
        return records
