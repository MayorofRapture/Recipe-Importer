"""Small, thread-safe model routing and best-effort local quota ledger."""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional


class QuotaExhausted(RuntimeError):
    pass


@dataclass(frozen=True)
class ModelConfig:
    id: str
    role: str = "primary"
    rpm: float = 0.0
    rpd: int = 0
    tpm: int = 0
    enabled: bool = True


@dataclass(frozen=True)
class RouteResult:
    value: Any
    model_id: str
    reason: str
    attempts: int


def parse_model_config(raw: Optional[str] = None, fallback_model: str = "") -> list[ModelConfig]:
    if not raw:
        if not fallback_model.strip():
            raise ValueError("At least one Gemini model must be configured")
        return [ModelConfig(fallback_model.strip(), "primary")]
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("GEMINI_MODELS_JSON must be valid JSON") from exc
    entries = parsed.get("models", parsed) if isinstance(parsed, dict) else parsed
    if not isinstance(entries, list) or not entries:
        raise ValueError("GEMINI_MODELS_JSON must contain a non-empty models list")
    models = []
    for entry in entries:
        if not isinstance(entry, dict) or not str(entry.get("id", "")).strip():
            raise ValueError("Each configured Gemini model needs a non-empty id")
        role = str(entry.get("role", "primary"))
        if role not in {"primary", "quality_escalation", "quota_fallback"}:
            raise ValueError(f"Unsupported Gemini model role: {role}")
        models.append(ModelConfig(
            id=str(entry["id"]).strip(), role=role,
            rpm=float(entry.get("rpm", 0) or 0),
            rpd=int(entry.get("rpd", 0) or 0),
            tpm=int(entry.get("tpm", 0) or 0),
            enabled=bool(entry.get("enabled", True)),
        ))
    return models


class UsageLedger:
    """Persist request starts; UTC date is a best-effort provider-day proxy."""
    def __init__(self, path: Path, now: Callable[[], datetime] | None = None):
        self.path = path
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.lock = threading.Lock()
        self.data = self._load()

    def _day(self) -> str:
        return self.now().astimezone(timezone.utc).date().isoformat()

    def _load(self) -> dict:
        if not self.path.exists():
            return {"day": self._day(), "models": {}}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"Cannot read Gemini usage ledger: {self.path}") from exc
        if data.get("day") != self._day():
            return {"day": self._day(), "models": {}}
        data.setdefault("models", {})
        return data

    def reserve(self, model: str, rpd: int = 0) -> None:
        with self.lock:
            if self.data.get("day") != self._day():
                self.data = {"day": self._day(), "models": {}}
            entry = self.data["models"].setdefault(model, {"requests": 0})
            if rpd and int(entry.get("requests", 0)) >= rpd:
                raise QuotaExhausted(f"Local daily request limit reached for {model}")
            entry["requests"] = int(entry.get("requests", 0)) + 1
            self._save()

    def count(self, model: str) -> int:
        with self.lock:
            return int(self.data.get("models", {}).get(model, {}).get("requests", 0))

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(self.path.suffix + ".tmp")
        temp.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
        os.replace(temp, self.path)


class ModelRouter:
    def __init__(self, models: list[ModelConfig], ledger: UsageLedger,
                 global_rpm: float = 0, sleep: Callable[[float], None] = time.sleep,
                 max_transient_retries: int = 2):
        self.models = [model for model in models if model.enabled]
        if not self.models:
            raise ValueError("At least one enabled Gemini model is required")
        self.ledger = ledger
        self.global_delay = 60.0 / global_rpm if global_rpm > 0 else 0.0
        self.next_global_start = 0.0
        self.lock = threading.Lock()
        self.next_model_start: dict[str, float] = {}
        self.model_lock = threading.Lock()
        self.sleep = sleep
        self.max_transient_retries = max_transient_retries

    def _wait_global(self) -> None:
        with self.lock:
            now = time.monotonic()
            start = max(now, self.next_global_start)
            self.next_global_start = start + self.global_delay
        if start > now:
            self.sleep(start - now)

    def _wait_model(self, model: ModelConfig) -> None:
        if model.rpm <= 0:
            return
        with self.model_lock:
            now = time.monotonic()
            start = max(now, self.next_model_start.get(model.id, 0.0))
            self.next_model_start[model.id] = start + 60.0 / model.rpm
        if start > now:
            self.sleep(start - now)

    def run(self, attempt: Callable[[ModelConfig], Any],
            quality_check: Callable[[Any], bool] | None = None) -> RouteResult:
        remaining = list(self.models)
        route_reason = "primary"
        total_attempts = 0
        last_error: Exception | None = None
        while remaining:
            model = remaining.pop(0)
            try:
                value = self._run_model(model, attempt)
                total_attempts += 1
                if quality_check is not None and quality_check(value):
                    quality = next((item for item in remaining if item.role == "quality_escalation"), None)
                    if quality is not None:
                        remaining.remove(quality)
                        remaining.insert(0, quality)
                        route_reason = "quality_escalation"
                        continue
                return RouteResult(value, model.id, route_reason if model is not self.models[0] else "primary", total_attempts)
            except QuotaExhausted as exc:
                last_error = exc
                total_attempts += 1
                route_reason = "quota_fallback"
            except Exception as exc:
                last_error = exc
                total_attempts += 1
                if not remaining:
                    break
                route_reason = "quality_escalation" if remaining[0].role == "quality_escalation" else "fallback"
        if last_error is None:
            raise QuotaExhausted("No eligible Gemini models remain")
        raise last_error

    def _run_model(self, model: ModelConfig, attempt: Callable[[ModelConfig], Any]) -> Any:
        tries = 0
        while True:
            self._wait_global()
            self._wait_model(model)
            self.ledger.reserve(model.id, model.rpd)
            try:
                return attempt(model)
            except Exception as exc:
                if _is_quota_error(exc):
                    raise QuotaExhausted(_safe_error(exc)) from exc
                if not _is_transient_error(exc) or tries >= self.max_transient_retries:
                    raise
                tries += 1
                self.sleep(min(60.0, 2 ** tries))


def _safe_error(exc: Exception) -> str:
    return str(exc).replace("GEMINI_API_KEY", "[redacted]").replace("MEALIE_TOKEN", "[redacted]")[:500]


def _is_quota_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(term in text for term in ("429", "quota", "resource_exhausted", "rate limit"))


def _is_transient_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(term in text for term in ("503", "502", "504", "unavailable", "timeout", "deadline exceeded", "temporarily"))


def validate_model_ids(client: Any, models: list[ModelConfig]) -> None:
    """Validate configured IDs against the provider's current model listing."""
    available = set()
    for item in client.models.list():
        name = getattr(item, "name", "") or ""
        available.add(name.removeprefix("models/"))
    missing = [item.id for item in models if item.id not in available]
    if missing:
        raise ValueError("Configured Gemini model IDs were not found: " + ", ".join(missing))
