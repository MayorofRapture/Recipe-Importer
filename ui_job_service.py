"""Durable extraction job lifecycle independent of Streamlit sessions."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import uuid
from typing import Any, Callable, Mapping

from batch_store import BatchStore, _LockBusy, _atomic_json, _file_lock


LOCAL_OCR = "local_ocr"
GEMINI = "gemini"
OCR_ASSISTED = "ocr_assisted"
_VALID_MODES = {LOCAL_OCR, GEMINI, OCR_ASSISTED}
_CLOUD_MODES = {GEMINI, OCR_ASSISTED}
_ACTIVE_STATES = {"queued", "waiting_for_worker", "running", "pausing", "paused", "quota_paused"}
_FAILED_RESULT_STATES = {"OCR_FAILED", "OCR_STRUCTURE_FAILED", "EXTRACTION_FAILED", "WORKER_FAILED"}
_NO_RECIPE_STATES = {"NO_RECIPE", "OCR_NO_RECIPE"}
_BACKGROUND_LOCK = threading.Lock()
_BACKGROUND_THREADS: dict[str, threading.Thread] = {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _fixture_mode() -> bool:
    return os.environ.get("RECIPE_IMPORTER_FIXTURE_MODE", "").strip().lower() in {
        "1", "true", "yes", "on",
    }


class JobService:
    """Manage one durable job per batch and serialize extraction workers."""

    def __init__(self, output_dir: Path | str, config: Mapping[str, Any] | None = None,
                 extractor: Callable[[dict, str, dict], Any] | None = None,
                 *, background: bool = False):
        self.output_dir = Path(output_dir).expanduser().resolve()
        self.root = self.output_dir / "ui_jobs"
        self.manifest_path = self.root / "jobs.json"
        self.manifest_lock = self.root / "locks" / "manifest.lock"
        self.global_worker_lock = self.root / "locks" / "extraction-worker.lock"
        self.config = dict(config) if config is not None else None
        self.extractor = extractor
        self.background = bool(background)

    def start(self, batch_id: str, mode: str, cloud_consent: bool = False) -> dict:
        mode = str(mode or "").strip().lower()
        if mode not in _VALID_MODES:
            raise ValueError("Unsupported extraction mode.")
        if mode in _CLOUD_MODES and not cloud_consent:
            raise ValueError("Cloud extraction requires explicit consent because recipe scans or OCR text may be sent to Google.")
        if _fixture_mode() and self.extractor is None:
            raise RuntimeError("Fixture mode requires an explicitly injected fake extractor; live services and workers are disabled.")

        batch = BatchStore(self.output_dir).get(str(batch_id))
        if batch is None:
            raise ValueError("Batch was not found.")
        if not batch.get("sources"):
            raise ValueError("Add at least one accepted source file before starting extraction.")
        if self.extractor is None:
            self.config = self._runtime_config(mode)

        timestamp = _now()
        job_id = str(uuid.uuid4())
        created = False
        with _file_lock(self.manifest_lock):
            manifest = self._read_manifest()
            existing = next((item for item in manifest["jobs"].values()
                             if item.get("batch_id") == str(batch_id)
                             and item.get("status") in _ACTIVE_STATES), None)
            if existing is not None:
                return _snapshot(existing)
            historical = sorted(
                (item for item in manifest["jobs"].values() if item.get("batch_id") == str(batch_id)),
                key=lambda item: (item.get("created_at", ""), item.get("job_id", "")),
                reverse=True,
            )
            processed_source_ids = {
                source.get("source_id")
                for prior in historical
                for source in prior.get("sources", [])
                if source.get("source_id")
            }
            pending_sources = [source for source in batch["sources"]
                               if source.get("source_id") not in processed_source_ids]
            if not pending_sources:
                return _snapshot(historical[0]) if historical else {}
            job = {
                "job_id": job_id,
                "batch_id": str(batch_id),
                "mode": mode,
                "status": "queued",
                "cloud_consent": bool(cloud_consent and mode in _CLOUD_MODES),
                "cloud_consent_at": timestamp if cloud_consent and mode in _CLOUD_MODES else None,
                "created_at": timestamp,
                "updated_at": timestamp,
                "started_at": None,
                "completed_at": None,
                "pause_requested": False,
                "current_source_id": None,
                "quota_paused": False,
                "quota_note": "",
                "error": "",
                "recipe_count": 0,
                "candidate_count": 0,
                "failure_count": 0,
                "sources": [
                    {
                        **source,
                        "status": "waiting",
                        "attempt_count": 0,
                        "attempts": [],
                        "results": [],
                        "error": "",
                    }
                    for source in pending_sources
                ],
            }
            manifest["jobs"][job_id] = job
            self._write_manifest(manifest)
            created = True

        if created:
            try:
                self._launch_worker(job_id)
            except Exception as exc:
                self._mutate_job(job_id, lambda value: _mark_job_failed(value, _redact(exc, self.config)))
        return self.snapshot(job_id) or _snapshot(job)

    def pause(self, job_id: str) -> dict:
        def pause_job(job: dict) -> None:
            if job["status"] in {"completed", "completed_with_errors", "failed"}:
                return
            job["pause_requested"] = True
            active_source = next((source for source in job["sources"] if source["status"] == "processing"), None)
            job["status"] = "pausing" if active_source else "paused"
            job["updated_at"] = _now()

        self._mutate_job(job_id, pause_job)
        return self.snapshot(job_id) or {}

    def resume(self, job_id: str) -> dict:
        job = self.snapshot(job_id)
        if job is None:
            raise ValueError("Job was not found.")
        if job["status"] == "completed":
            return job
        if self.extractor is None and _fixture_mode():
            raise RuntimeError("Fixture mode requires an explicitly injected fake extractor; live services and workers are disabled.")

        if job["status"] in {"failed", "completed_with_errors"}:
            # Terminal worker failure and partial source failure are retryable
            # through the same durable job. The job lock prevents a retry from
            # racing a worker that is still unwinding after a failed status write.
            try:
                with _file_lock(self.root / "locks" / f"job-{job_id}.lock", blocking=False):
                    self._mutate_job(job_id, _recover_interrupted)
                    self._mutate_job(job_id, _prepare_resume)
            except _LockBusy:
                return self.snapshot(job_id) or job
            current = self.snapshot(job_id) or job
            if current.get("status") == "queued":
                try:
                    self._launch_worker(job_id)
                except Exception as exc:
                    self._mutate_job(job_id, lambda value: _mark_job_failed(value, _redact(exc, self.config)))
            return self.snapshot(job_id) or current

        if job["status"] in {"running", "pausing", "queued", "waiting_for_worker"}:
            # A process can die after persisting "running". The per-job OS lock
            # distinguishes that stale state from a live worker without keeping
            # the shared manifest lock held.
            try:
                with _file_lock(self.root / "locks" / f"job-{job_id}.lock", blocking=False):
                    self._mutate_job(job_id, _recover_interrupted)
                    self._mutate_job(job_id, _prepare_resume)
            except _LockBusy:
                return self.snapshot(job_id) or job
            current = self.snapshot(job_id) or job
            if current.get("status") == "queued":
                try:
                    self._launch_worker(job_id)
                except Exception as exc:
                    self._mutate_job(job_id, lambda value: _mark_job_failed(value, _redact(exc, self.config)))
            return self.snapshot(job_id) or current

        def resume_job(current: dict) -> None:
            if current["status"] == "completed":
                return
            for source in current["sources"]:
                if source["status"] in {"failed", "quota_paused"}:
                    source["status"] = "waiting"
                    source["error"] = ""
            current["pause_requested"] = False
            current["quota_paused"] = False
            current["quota_note"] = ""
            current["status"] = "queued"
            current["updated_at"] = _now()
            current["completed_at"] = None

        self._mutate_job(job_id, resume_job)
        current = self.snapshot(job_id) or {}
        if current.get("status") == "queued":
            try:
                self._launch_worker(job_id)
            except Exception as exc:
                self._mutate_job(job_id, lambda value: _mark_job_failed(value, _redact(exc, self.config)))
        return self.snapshot(job_id) or current

    def snapshot(self, job_id: str) -> dict | None:
        with _file_lock(self.manifest_lock):
            job = self._read_manifest()["jobs"].get(str(job_id))
            return _snapshot(job) if job is not None else None

    def list_jobs(self) -> list[dict]:
        with _file_lock(self.manifest_lock):
            jobs = list(self._read_manifest()["jobs"].values())
        return sorted((_snapshot(job) for job in jobs),
                      key=lambda item: (item.get("created_at", ""), item["job_id"]), reverse=True)

    def run_pending(self, job_id: str) -> dict:
        """Process unresolved sources synchronously; production worker calls this method."""
        job = self.snapshot(job_id)
        if job is None:
            raise ValueError("Job was not found.")
        if self.extractor is None:
            if _fixture_mode():
                raise RuntimeError("Fixture mode requires an explicitly injected fake extractor; live services and workers are disabled.")
            if self.config is None:
                self.config = self._runtime_config(job["mode"])
        with _file_lock(self.root / "locks" / f"job-{job_id}.lock", blocking=False):
            with self._worker_lease(job_id) as acquired:
                if not acquired:
                    return self.snapshot(job_id) or job
                self._mutate_job(job_id, _recover_interrupted)
                self._mutate_job(job_id, _begin_job)
                while True:
                    current = self.snapshot(job_id)
                    if current is None:
                        raise RuntimeError("Job disappeared while it was running.")
                    if current["pause_requested"]:
                        self._mutate_job(job_id, _mark_paused)
                        break
                    source = next((item for item in current["sources"] if item["status"] == "waiting"), None)
                    if source is None:
                        break
                    source_id = source["source_id"]
                    attempt_started = _now()
                    self._mutate_job(job_id, lambda value: _begin_source(value, source_id, attempt_started))
                    try:
                        rows = self._extract(source, current["mode"])
                        outcome = _source_outcome(rows)
                        self._mutate_job(job_id, lambda value: _finish_source(
                            value, source_id, rows, outcome, attempt_started))
                    except Exception as exc:
                        self._mutate_job(job_id, lambda value: _fail_source(
                            value, source_id, _redact(exc, self.config), attempt_started))
                    latest = self.snapshot(job_id)
                    if latest and latest["quota_paused"]:
                        break
                self._mutate_job(job_id, _finish_job)
        return self.snapshot(job_id) or {}

    def _extract(self, source: dict, mode: str) -> list[dict]:
        if self.extractor is not None:
            result = self.extractor(dict(source), mode, dict(self.config or {}))
            batch = _as_extraction_batch(result)
            if batch is not None:
                return _publish_extraction_batch(self.output_dir, source, batch, mode)
            return _normalize_result(result, source)
        return _default_extract(self.output_dir, source, mode, dict(self.config or {}))

    @contextmanager
    def _worker_lease(self, job_id: str):
        """Wait for the single extraction owner, allowing pause while queued."""
        while True:
            current = self.snapshot(job_id)
            if current is None or current["pause_requested"]:
                yield False
                return
            lease = _file_lock(self.global_worker_lock, blocking=False)
            try:
                lease.__enter__()
                break
            except _LockBusy:
                self._mutate_job(job_id, _mark_waiting_for_worker)
                time.sleep(0.1)
        try:
            yield True
        finally:
            lease.__exit__(None, None, None)

    def _runtime_config(self, mode: str) -> dict:
        if self.config is not None:
            config = dict(self.config)
        else:
            from dotenv import load_dotenv
            load_dotenv()
            config = {}
        if mode not in _CLOUD_MODES:
            return {
                "tesseract_cmd": str(config.get("tesseract_cmd", os.getenv("TESSERACT_CMD", ""))).strip(),
                "global_rpm": float(config.get("global_rpm", os.getenv("GEMINI_RPM", "12"))),
            }
        api_key = str(config.get("api_key", config.get("GEMINI_API_KEY", os.getenv("GEMINI_API_KEY", "")))).strip()
        model = str(config.get("model", config.get("GEMINI_MODEL", os.getenv("GEMINI_MODEL", "")))).strip()
        raw_models = config.get("models_json", config.get("GEMINI_MODELS_JSON", os.getenv("GEMINI_MODELS_JSON")))
        if not api_key:
            raise ValueError("Gemini extraction requires a configured GEMINI_API_KEY.")
        from model_routing import parse_model_config
        models = parse_model_config(raw_models, model)
        if not models:
            raise ValueError("At least one Gemini model must be configured.")
        rpm = float(config.get("global_rpm", config.get("GEMINI_RPM", os.getenv("GEMINI_RPM", "12"))))
        if rpm <= 0:
            raise ValueError("Configured Gemini request rate must be greater than zero.")
        return {
            "api_key": api_key,
            "model": model or models[0].id,
            "models_json": raw_models,
            "tesseract_cmd": str(config.get("tesseract_cmd", os.getenv("TESSERACT_CMD", ""))).strip(),
            "global_rpm": rpm,
            "validate_models": bool(config.get("validate_models", False)),
        }

    def _launch_worker(self, job_id: str) -> None:
        if self.extractor is not None:
            if self.background:
                self._launch_background_fixture_worker(job_id)
            return
        if _fixture_mode():
            raise RuntimeError("Fixture mode requires an explicitly injected fake extractor; live services and workers are disabled.")
        worker = Path(__file__).resolve().parent / "tools" / "extraction_worker.py"
        if not worker.is_file():
            raise RuntimeError("Extraction worker entrypoint is unavailable.")
        env = dict(os.environ)
        if self.config:
            for source_key, env_key in (
                ("api_key", "GEMINI_API_KEY"), ("model", "GEMINI_MODEL"),
                ("models_json", "GEMINI_MODELS_JSON"), ("global_rpm", "GEMINI_RPM"),
                ("tesseract_cmd", "TESSERACT_CMD"),
            ):
                if source_key in self.config:
                    env[env_key] = str(self.config[source_key])
        args = [sys.executable, str(worker), "--output-dir", str(self.output_dir), "--job-id", str(job_id)]
        options: dict[str, Any] = {
            "cwd": str(Path(__file__).resolve().parent),
            "env": env,
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "close_fds": True,
        }
        if os.name == "nt":
            options["creationflags"] = (getattr(subprocess, "DETACHED_PROCESS", 0)
                                         | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
            startup = subprocess.STARTUPINFO()
            startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            options["startupinfo"] = startup
        else:
            options["start_new_session"] = True
        subprocess.Popen(args, **options)

    def _launch_background_fixture_worker(self, job_id: str) -> None:
        """Keep explicitly injected fixture work off Streamlit's rerun thread."""
        with _BACKGROUND_LOCK:
            existing = _BACKGROUND_THREADS.get(str(job_id))
            if existing is not None and existing.is_alive():
                return

            def run() -> None:
                try:
                    self.run_pending(job_id)
                except Exception as exc:
                    try:
                        self._mutate_job(job_id, lambda value: _mark_job_failed(value, _redact(exc, self.config)))
                    except Exception:
                        pass
                finally:
                    with _BACKGROUND_LOCK:
                        if _BACKGROUND_THREADS.get(str(job_id)) is threading.current_thread():
                            _BACKGROUND_THREADS.pop(str(job_id), None)

            thread = threading.Thread(target=run, name=f"recipe-fixture-job-{job_id[:8]}", daemon=True)
            _BACKGROUND_THREADS[str(job_id)] = thread
            thread.start()

    def _read_manifest(self) -> dict:
        if not self.manifest_path.exists():
            return {"schema_version": 1, "jobs": {}}
        try:
            data = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError("Extraction job history is unavailable; existing job data was preserved.") from exc
        if not isinstance(data, dict) or not isinstance(data.get("jobs"), dict):
            raise RuntimeError("Extraction job history has an unsupported shape.")
        return data

    def _write_manifest(self, data: dict) -> None:
        _atomic_json(self.manifest_path, data)

    def _mutate_job(self, job_id: str, operation: Callable[[dict], None]) -> dict:
        with _file_lock(self.manifest_lock):
            manifest = self._read_manifest()
            job = manifest["jobs"].get(str(job_id))
            if job is None:
                raise ValueError("Job was not found.")
            operation(job)
            job["updated_at"] = _now()
            _recount(job)
            self._write_manifest(manifest)
            return _snapshot(job)


def _snapshot(job: dict) -> dict:
    value = json.loads(json.dumps(job))
    total = len(value.get("sources", []))
    terminal = {"completed", "no_recipe", "failed", "quota_paused"}
    completed = sum(1 for source in value.get("sources", []) if source.get("status") in terminal)
    value["progress"] = {"total": total, "completed": completed, "remaining": max(0, total - completed)}
    value["sources"] = sorted(value.get("sources", []), key=lambda item: (item.get("created_at", ""), item["source_id"]))
    return value


def _recount(job: dict) -> None:
    candidate_keys: set[str] = set()
    recipe_count = 0
    failure_count = 0
    for source in job.get("sources", []):
        if source.get("status") == "failed":
            failure_count += 1
        for index, row in enumerate(source.get("results", [])):
            if str(row.get("status", "")).upper() in _FAILED_RESULT_STATES | _NO_RECIPE_STATES:
                continue
            if row.get("status") and (row.get("candidate_id") or row.get("title")):
                identity = str(row.get("candidate_id") or f"{source['source_id']}:{row.get('title')}:{index}")
                candidate_keys.add(identity)
    recipe_count = len(candidate_keys)
    job["recipe_count"] = recipe_count
    job["candidate_count"] = recipe_count
    job["failure_count"] = failure_count


def _begin_job(job: dict) -> None:
    if job["status"] in {"completed", "completed_with_errors", "failed", "paused", "quota_paused"}:
        return
    job["status"] = "running"
    job["started_at"] = job.get("started_at") or _now()
    job["updated_at"] = _now()
    job["error"] = ""


def _mark_waiting_for_worker(job: dict) -> None:
    if job["status"] == "queued" and not job.get("pause_requested"):
        job["status"] = "waiting_for_worker"
        job["updated_at"] = _now()


def _recover_interrupted(job: dict) -> None:
    """Return only unfinished file work to the queue after an owner exits."""
    for source in job.get("sources", []):
        if source.get("status") != "processing":
            continue
        source["status"] = "waiting"
        source["error"] = "Previous extraction worker stopped before this source completed."
        attempts = source.get("attempts", [])
        if attempts and attempts[-1].get("status") == "processing":
            attempts[-1].update({
                "status": "interrupted",
                "completed_at": _now(),
                "error": source["error"],
            })
    job["current_source_id"] = None
    if job.get("status") in {"running", "pausing", "waiting_for_worker"}:
        job["status"] = "paused" if job.get("pause_requested") else "queued"


def _prepare_resume(job: dict) -> None:
    if job["status"] == "completed":
        return
    for source in job.get("sources", []):
        if source["status"] in {"failed", "quota_paused"}:
            source["status"] = "waiting"
            source["error"] = ""
    job["pause_requested"] = False
    job["quota_paused"] = False
    job["quota_note"] = ""
    job["completed_at"] = None
    if any(source["status"] == "waiting" for source in job.get("sources", [])):
        job["status"] = "queued"
    else:
        _finish_job(job)
    job["updated_at"] = _now()


def _begin_source(job: dict, source_id: str, started_at: str) -> None:
    source = _find_source(job, source_id)
    source["status"] = "processing"
    source["error"] = ""
    source["attempt_count"] = int(source.get("attempt_count", 0)) + 1
    source["attempts"].append({"attempt": source["attempt_count"], "started_at": started_at, "status": "processing"})
    job["current_source_id"] = source_id
    job["status"] = "running"
    job["updated_at"] = _now()


def _finish_source(job: dict, source_id: str, rows: list[dict], outcome: str, started_at: str) -> None:
    source = _find_source(job, source_id)
    source["results"] = _safe_rows(rows)
    source["status"] = outcome
    attempt = source["attempts"][-1]
    attempt.update({"status": outcome, "completed_at": _now(), "result_count": len(rows)})
    job["current_source_id"] = None
    if outcome == "quota_paused":
        job["quota_paused"] = True
        job["quota_note"] = _quota_note(rows)
        job["status"] = "quota_paused"
    elif job.get("pause_requested"):
        job["status"] = "paused"
    job["updated_at"] = _now()


def _fail_source(job: dict, source_id: str, error: str, started_at: str) -> None:
    source = _find_source(job, source_id)
    source["status"] = "failed"
    source["error"] = error[:500]
    source["results"] = [{"status": "WORKER_FAILED", "title": "", "note": error[:500]}]
    attempt = source["attempts"][-1]
    attempt.update({"status": "failed", "completed_at": _now(), "error": error[:500]})
    job["current_source_id"] = None
    if job.get("pause_requested"):
        job["status"] = "paused"
    job["updated_at"] = _now()


def _mark_paused(job: dict) -> None:
    job["status"] = "paused"
    job["pause_requested"] = True
    job["current_source_id"] = None
    job["updated_at"] = _now()


def _mark_job_failed(job: dict, error: str) -> None:
    job["status"] = "failed"
    job["error"] = error[:500]
    job["completed_at"] = _now()
    job["updated_at"] = _now()


def _finish_job(job: dict) -> None:
    job["current_source_id"] = None
    if job.get("quota_paused"):
        job["status"] = "quota_paused"
        job["completed_at"] = None
    elif job.get("pause_requested"):
        job["status"] = "paused"
        job["completed_at"] = None
    elif any(source.get("status") == "waiting" for source in job.get("sources", [])):
        job["status"] = "queued"
        job["completed_at"] = None
    elif any(source.get("status") == "failed" for source in job.get("sources", [])):
        job["status"] = "completed_with_errors"
        job["completed_at"] = _now()
    else:
        job["status"] = "completed"
        job["completed_at"] = _now()
    job["updated_at"] = _now()


def _find_source(job: dict, source_id: str) -> dict:
    source = next((item for item in job["sources"] if item["source_id"] == source_id), None)
    if source is None:
        raise ValueError("Source was not found in this job.")
    return source


def _normalize_result(result: Any, source: dict) -> list[dict]:
    if isinstance(result, list):
        return [dict(item) for item in result if isinstance(item, Mapping)]
    recipes = getattr(result, "recipes", None)
    if recipes is None and hasattr(result, "model_dump"):
        dumped = result.model_dump(mode="json")
        recipes = dumped.get("recipes", []) if isinstance(dumped, dict) else None
    if recipes is None:
        raise TypeError("Extractor must return report rows or an ExtractionBatch.")
    rows = []
    for candidate in recipes:
        name = getattr(candidate, "name", "") or (candidate.get("name", "") if isinstance(candidate, Mapping) else "")
        candidate_id = getattr(candidate, "candidate_id", "") or (candidate.get("candidate_id", "") if isinstance(candidate, Mapping) else "")
        rows.append({"status": "EXTRACTED_ONLY", "title": str(name), "candidate_id": str(candidate_id)})
    return rows


def _safe_rows(rows: list[dict]) -> list[dict]:
    allowed = {
        "status", "title", "candidate_id", "model", "gemini_model", "route_reason",
        "note", "ocr_status", "ocr_engine", "ocr_text_path", "structuring_method",
        "recipe_count", "confidence", "review_status", "manual_review_required",
        "failure_reason", "attempted_models", "diagnostic_log",
    }
    safe = []
    for row in rows:
        item = {key: row[key] for key in allowed if key in row and isinstance(row[key], (str, int, float, bool, list, type(None)))}
        if isinstance(item.get("note"), str):
            item["note"] = item["note"][:500]
        safe.append(item)
    return safe


def _source_outcome(rows: list[dict]) -> str:
    statuses = {str(row.get("status", "")).upper() for row in rows}
    if any(_is_quota_result(row) for row in rows):
        return "quota_paused"
    if statuses & _FAILED_RESULT_STATES:
        return "failed"
    if statuses and statuses <= _NO_RECIPE_STATES:
        return "no_recipe"
    return "completed"


def _is_quota_result(row: dict) -> bool:
    status = str(row.get("status", "")).upper()
    reason = str(row.get("route_reason", "")).casefold()
    text = f"{row.get('note', '')} {row.get('failure_reason', '')}".casefold()
    return status in {"QUOTA_EXHAUSTED", "PAUSED_QUOTA"} or (
        status in _FAILED_RESULT_STATES and (
            "quota" in text or "resource_exhausted" in text
            or reason in {"quota", "quota_pause", "quota_fallback"}
        )
    )


def _quota_note(rows: list[dict]) -> str:
    return next((str(row.get("note") or row.get("failure_reason") or "")[:500]
                 for row in rows if _is_quota_result(row)), "Request allowance reached; resume when quota is available.")


def _redact(error: Exception, config: Mapping[str, Any] | None) -> str:
    message = str(error)
    if config:
        for key, value in config.items():
            if "key" in str(key).casefold() or "token" in str(key).casefold():
                secret = str(value or "")
                if secret:
                    message = message.replace(secret, "[redacted]")
    for env_name in ("GEMINI_API_KEY", "MEALIE_TOKEN"):
        secret = os.environ.get(env_name, "")
        if secret:
            message = message.replace(secret, "[redacted]")
    return message[:500]


def _default_extract(output_dir: Path, source: dict, mode: str, config: dict) -> list[dict]:
    from hashlib import sha256

    source_path = Path(source["staged_path"])
    content_digest = sha256(source_path.read_bytes()).hexdigest()
    if content_digest != source["digest"]:
        raise RuntimeError("Staged source digest changed; extraction was stopped.")
    from import_recipes import (
        StateStore, _ocr_only_file, _ocr_structure_file, file_digest, process_file,
    )
    from review_store import ReviewStore
    from recipe_quality import RecipeCandidate
    reviews = ReviewStore(output_dir / "review_events.jsonl")
    if mode == LOCAL_OCR:
        rows = _ocr_only_file(source_path, content_digest, output_dir,
                              str(config.get("tesseract_cmd", "")), reviews)
        candidates = [
            RecipeCandidate.model_validate(reviews.candidates[row["candidate_id"]]["candidate"])
            for row in rows if row.get("candidate_id") in reviews.candidates
        ]
        if candidates:
            from recipe_quality import ExtractionBatch
            return _publish_extraction_batch(output_dir, source, ExtractionBatch(recipes=candidates), mode, rows)
        return rows

    from runtime_safety import require_live_services
    require_live_services()
    from model_routing import ModelRouter, UsageLedger, parse_model_config, validate_model_ids
    api_key = str(config["api_key"])
    models = parse_model_config(config.get("models_json"), str(config.get("model", "")))
    if config.get("validate_models"):
        from import_recipes import google_client
        validate_model_ids(google_client(api_key), models)
    router = ModelRouter(
        models,
        UsageLedger(output_dir / "gemini_usage.json"),
        global_rpm=float(config.get("global_rpm", 12)),
    )
    if mode == OCR_ASSISTED:
        return _ocr_structure_file(source_path, content_digest, output_dir, config, router, reviews)
    if mode != GEMINI:
        raise ValueError("Unsupported extraction mode.")
    state = StateStore(output_dir / "import_state.jsonl")
    return process_file(
        source_path, file_digest(source_path), config, None, state,
        output_dir / "extracted", True, False, False, reviews, router,
        batch_tag="", mealie_client=None,
    )


def _as_extraction_batch(result: Any):
    from recipe_quality import ExtractionBatch

    if isinstance(result, ExtractionBatch):
        return result
    if isinstance(result, Mapping):
        payload = result.get("batch", result)
        if isinstance(payload, Mapping) and "recipes" in payload:
            try:
                return ExtractionBatch.model_validate(payload)
            except Exception:
                return None
    if hasattr(result, "recipes") or hasattr(result, "model_dump"):
        try:
            payload = result.model_dump(mode="json") if hasattr(result, "model_dump") else result
            return ExtractionBatch.model_validate(payload)
        except Exception:
            return None
    return None


def _publish_extraction_batch(output_dir: Path, source: dict, batch, method: str,
                              report_rows: list[dict] | None = None) -> list[dict]:
    """Persist an immutable, source-linked cache that the review catalog can load."""
    from recipe_quality import ExtractionBatch
    from review_store import ReviewStore, extraction_identity

    if not isinstance(batch, ExtractionBatch):
        batch = ExtractionBatch.model_validate(batch)
    digest = str(source["digest"])
    cache_dir = output_dir / "extracted"
    cache_dir.mkdir(parents=True, exist_ok=True)
    extraction_id = extraction_identity(digest, batch.model_dump(mode="json"))
    cache_path = cache_dir / f"{digest}--ui-{uuid.uuid4().hex}.json"
    _atomic_json(cache_path, {
        "schema_version": 2,
        "source_hash": digest,
        "extraction_id": extraction_id,
        "extraction_method": method,
        "attempt_id": str(uuid.uuid4()),
        "batch": batch.model_dump(mode="json"),
    })

    reviews = ReviewStore(output_dir / "review_events.jsonl")
    registrations = reviews.register_candidates(
        str(source["staged_path"]), digest, extraction_id, batch.recipes,
    )
    rows = []
    prior_rows = report_rows or []
    default_status = "OCR_NEEDS_REVIEW" if method == LOCAL_OCR else "EXTRACTED_ONLY"
    for index, (candidate, registration) in enumerate(zip(batch.recipes, registrations)):
        prior = prior_rows[index] if index < len(prior_rows) else {}
        row = dict(prior)
        row.update({
            "status": prior.get("status") or default_status,
            "title": candidate.name,
            "candidate_id": registration["candidate_id"],
            "route_reason": method,
            "recipe_count": len(batch.recipes),
        })
        rows.append(row)
    return rows
