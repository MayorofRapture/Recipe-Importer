"""Small, thread-safe model routing and best-effort local quota ledger."""
from __future__ import annotations

import json
import os
import threading
import time
from contextlib import contextmanager
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
    """Persist daily reservations and shared request-start windows.

    The short file lock protects only the local read/modify/write transaction. It
    is released before waiting and before the provider request is made.
    """
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
        self._reserve(model, rpd, model_rpm=0, global_rpm=0, sleep=time.sleep)

    def reserve_scheduled(self, model: str, rpd: int = 0, *, model_rpm: float = 0,
                          global_rpm: float = 0,
                          tpm: int = 0, tokens: int = 0,
                          sleep: Callable[[float], None] = time.sleep) -> None:
        """Reserve one daily request and a shared global/per-model start slot."""
        self._reserve(model, rpd, model_rpm=model_rpm, global_rpm=global_rpm,
                      tpm=tpm, tokens=tokens, sleep=sleep)

    def _reserve(self, model: str, rpd: int, *, model_rpm: float, global_rpm: float,
                 tpm: int = 0, tokens: int = 0,
                 sleep: Callable[[float], None]) -> None:
        global_interval = 60.0 / global_rpm if global_rpm > 0 else 0.0
        model_interval = 60.0 / model_rpm if model_rpm > 0 else 0.0
        while True:
            wait = 0.0
            with self.lock, _process_file_lock(self.path.with_suffix(self.path.suffix + ".lock")):
                self.data = self._read_current()
                entry = self.data["models"].setdefault(model, {"requests": 0})
                if rpd and int(entry.get("requests", 0)) >= rpd:
                    raise QuotaExhausted(f"Local daily request limit reached for {model}")

                now = self.now().astimezone(timezone.utc).timestamp()
                model_windows = self.data.setdefault("model_windows", {})
                model_intervals = self.data.setdefault("model_intervals", {})
                shared_global_interval = max(global_interval,
                                             float(self.data.get("global_interval", 0)))
                shared_model_interval = max(model_interval,
                                            float(model_intervals.get(model, 0)))
                start = max(now, float(self.data.get("global_next_start", 0)),
                            float(model_windows.get(model, 0)))
                wait = max(0.0, start - now)
                token_windows = self.data.setdefault("model_token_windows", {}).setdefault(model, [])
                token_windows[:] = [item for item in token_windows if now - float(item[0]) < 60]
                if tpm and tokens > tpm:
                    raise QuotaExhausted(f"Suggestion request exceeds configured TPM allowance for {model}")
                if tpm and sum(int(item[1]) for item in token_windows) + tokens > tpm:
                    required = sum(int(item[1]) for item in token_windows) + tokens - tpm
                    elapsed_tokens = 0
                    token_wait = 60.0
                    for timestamp, count in token_windows:
                        elapsed_tokens += int(count)
                        if elapsed_tokens >= required:
                            token_wait = max(0.0, float(timestamp) + 60 - now)
                            break
                    wait = max(wait, token_wait)
                if not wait:
                    entry["requests"] = int(entry.get("requests", 0)) + 1
                    if shared_global_interval:
                        self.data["global_interval"] = shared_global_interval
                        self.data["global_next_start"] = start + shared_global_interval
                    if shared_model_interval:
                        model_intervals[model] = shared_model_interval
                        model_windows[model] = start + shared_model_interval
                    if tpm and tokens:
                        token_windows.append([now, int(tokens)])
                    self._save()
                    self.data = self._read_current()
                    return
            # Never hold either lock while waiting; every wake-up rechecks state.
            sleep(wait)

    def count(self, model: str) -> int:
        with self.lock, _process_file_lock(self.path.with_suffix(self.path.suffix + ".lock")):
            self.data = self._read_current()
            return int(self.data.get("models", {}).get(model, {}).get("requests", 0))

    def _read_current(self) -> dict:
        data = self._load()
        data.setdefault("models", {})
        data.setdefault("model_windows", {})
        return data

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(self.path.suffix + ".tmp")
        temp.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
        os.replace(temp, self.path)


@contextmanager
def _process_file_lock(path: Path):
    """Lock one byte in a sibling file across processes on Windows and POSIX."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class ModelRouter:
    def __init__(self, models: list[ModelConfig], ledger: UsageLedger,
                 global_rpm: float = 0, sleep: Callable[[float], None] = time.sleep,
                 max_transient_retries: int = 2):
        self.models = [model for model in models if model.enabled]
        if not self.models:
            raise ValueError("At least one enabled Gemini model is required")
        self.ledger = ledger
        self.global_rpm = global_rpm
        self.sleep = sleep
        self.max_transient_retries = max_transient_retries

    def run(self, attempt: Callable[[ModelConfig], Any],
            quality_check: Callable[[Any], bool] | None = None,
            *, retry_transient: bool = True, stop_on_transient: bool = False,
            request_tokens: int = 0) -> RouteResult:
        remaining = list(self.models)
        route_reason = "primary"
        total_attempts = 0
        attempted_models: list[str] = []
        last_error: Exception | None = None
        while remaining:
            model = remaining.pop(0)
            attempted_models.append(model.id)
            try:
                value = self._run_model(model, attempt, retry_transient=retry_transient,
                                        stop_on_transient=stop_on_transient,
                                        request_tokens=request_tokens)
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
                _attach_route_context(exc, attempted_models, model.id, route_reason)
                fallback = next((item for item in remaining if item.role == "quota_fallback"), None)
                if fallback is not None:
                    remaining.remove(fallback)
                    remaining.insert(0, fallback)
                    route_reason = "quota_fallback"
                else:
                    # A quality model is reserved for quality-driven escalation.
                    # Older configurations with multiple primary entries still
                    # retain their ordered primary fallback behavior.
                    remaining = [item for item in remaining if item.role == "primary"]
                    route_reason = "fallback"
            except Exception as exc:
                last_error = exc
                total_attempts += 1
                _attach_route_context(exc, attempted_models, model.id, route_reason)
                if getattr(exc, "stop_routing", False):
                    break
                if not remaining:
                    break
                route_reason = "quality_escalation" if remaining[0].role == "quality_escalation" else "fallback"
        if last_error is None:
            raise QuotaExhausted("No eligible Gemini models remain")
        raise last_error

    def _run_model(self, model: ModelConfig, attempt: Callable[[ModelConfig], Any], *,
                   retry_transient: bool = True, stop_on_transient: bool = False,
                   request_tokens: int = 0) -> Any:
        tries = 0
        while True:
            self.ledger.reserve_scheduled(model.id, model.rpd, model_rpm=model.rpm,
                                          global_rpm=self.global_rpm, tpm=model.tpm,
                                          tokens=request_tokens, sleep=self.sleep)
            try:
                return attempt(model)
            except Exception as exc:
                if _is_quota_error(exc):
                    raise QuotaExhausted(_safe_error(exc)) from exc
                if not retry_transient and stop_on_transient and _is_transient_error(exc):
                    try:
                        setattr(exc, "stop_routing", True)
                    except Exception:
                        pass
                    raise
                if not retry_transient or not _is_transient_error(exc) or tries >= self.max_transient_retries:
                    raise
                tries += 1
                self.sleep(min(60.0, 2 ** tries))


def _attach_route_context(exc: Exception, attempted_models: list[str],
                          model_id: str, route_reason: str) -> None:
    """Preserve routing context on errors without coupling the router to extraction code."""
    try:
        setattr(exc, "attempted_models", list(attempted_models))
        setattr(exc, "model", model_id)
        setattr(exc, "route_reason", route_reason)
    except Exception:
        pass


def _safe_error(exc: Exception) -> str:
    return str(exc).replace("GEMINI_API_KEY", "[redacted]").replace("MEALIE_TOKEN", "[redacted]")[:500]


def _is_quota_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(term in text for term in ("429", "quota", "resource_exhausted", "rate limit"))


def _is_transient_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(term in text for term in ("503", "502", "504", "unavailable", "timeout", "timed out", "deadline exceeded", "temporarily"))


def validate_model_ids(client: Any, models: list[ModelConfig]) -> None:
    """Validate configured IDs against the provider's current model listing."""
    available = set()
    for item in client.models.list():
        name = getattr(item, "name", "") or ""
        available.add(name.removeprefix("models/"))
    missing = [item.id for item in models if item.id not in available]
    if missing:
        raise ValueError("Configured Gemini model IDs were not found: " + ", ".join(missing))
