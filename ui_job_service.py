"""Durable extraction job lifecycle independent of Streamlit sessions."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from hashlib import sha256
import json
import marshal
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
import uuid
from types import CodeType
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
        self.range_manifest_path = self.root / "range_jobs.json"
        self.range_manifest_lock = self.root / "locks" / "range-jobs.lock"
        self.global_worker_lock = self.root / "locks" / "extraction-worker.lock"
        self.config = dict(config) if config is not None else None
        self.extractor = extractor
        self.background = bool(background)
        self._unversioned_range_scope = uuid.uuid4().hex

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

    def start_range_job(self, batch_id: str, range_ids: list[str], mode: str,
                        cloud_consent: bool = False) -> dict:
        """Persist an explicitly synthetic or production job for verified ranges."""
        mode = str(mode or "").strip().lower()
        if mode not in _VALID_MODES:
            raise ValueError("Unsupported extraction mode.")
        if mode in _CLOUD_MODES and not cloud_consent:
            raise ValueError("Cloud range extraction requires explicit consent.")
        if _fixture_mode() and self.extractor is None:
            raise RuntimeError("Fixture mode requires an explicitly injected synthetic extractor.")
        production = self.extractor is None
        if production:
            self.config = self._production_config(mode)
        extractor = self.extractor or _production_range_marker
        if not isinstance(range_ids, list) or not range_ids or any(not isinstance(item, str) or not item for item in range_ids):
            raise ValueError("Select one or more page ranges.")
        if len(set(range_ids)) != len(range_ids):
            raise ValueError("A page range may be selected only once.")

        store = BatchStore(self.output_dir)
        verified_ranges = [store.validate_page_range(batch_id, range_id) for range_id in range_ids]
        verified_ranges.sort(key=lambda item: (item["parent_document_id"], item["first_page"], item["last_page"]))
        config = _range_config_snapshot(self.config or {}, extractor, None if production else self._unversioned_range_scope)
        config_json = json.dumps(config, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        config_fingerprint = sha256(config_json.encode("utf-8")).hexdigest()
        versioned = _range_extractor_is_versioned(config)
        timestamp = _now()
        selected_ids = [item["range_id"] for item in verified_ranges]

        with _file_lock(self.range_manifest_lock):
            manifest = self._read_range_manifest()
            prior = sorted((job for job in manifest["jobs"].values()
                            if job.get("batch_id") == str(batch_id)
                            and job.get("range_ids") == selected_ids
                            and job.get("mode") == mode
                            and job.get("config_fingerprint") == config_fingerprint),
                           key=lambda job: (job.get("created_at", ""), job.get("job_id", "")),
                           reverse=True)
            active = next((job for job in prior if job.get("status") in _ACTIVE_STATES), None)
            if active is not None:
                return _range_snapshot(active)
            if versioned and prior and prior[0].get("status") == "completed":
                prior_job = prior[0]
                caches = [self._find_job_range_cache(item, prior_job)
                          for item in prior_job.get("ranges", [])]
                if all(caches):
                    return _range_snapshot(prior_job)

            job_id = str(uuid.uuid4())
            ranges = []
            for item in verified_ranges:
                cache_identity = _range_cache_identity(item, str(batch_id), mode, config_fingerprint)
                cache_context = _range_cache_context(
                    item, str(batch_id), mode, config_fingerprint, versioned, job_id)
                cache_context["execution_kind"] = "production" if production else "synthetic"
                cached = self._find_range_cache(cache_identity, cache_context)
                ranges.append({
                    "parent_document_id": item["parent_document_id"],
                    "parent_sha256": item["parent_sha256"],
                    "range_id": item["range_id"],
                    "first_page": item["first_page"],
                    "last_page": item["last_page"],
                    "page_count": item["page_count"],
                    "input_path": item["input_path"],
                    "input_sha256": item["input_sha256"],
                    "size_bytes": item["size_bytes"],
                    "mode": mode,
                    "config_fingerprint": config_fingerprint,
                    "cache_identity": cache_identity,
                    "status": cached.get("status", "completed") if cached else "waiting",
                    "attempt_count": 0,
                    "attempts": [],
                    "result_count": cached["result_count"] if cached else 0,
                    "cache_path": cached["cache_path"] if cached else "",
                    "error": "",
                    "created_at": timestamp,
                    "started_at": None,
                    "completed_at": timestamp if cached else None,
                })
            job = {
                "job_id": job_id,
                "job_type": "page_ranges",
                "batch_id": str(batch_id),
                "range_ids": selected_ids,
                "mode": mode,
                "execution_kind": "production" if production else "synthetic",
                "cloud_consent": bool(cloud_consent and mode in _CLOUD_MODES),
                "cloud_consent_at": timestamp if cloud_consent and mode in _CLOUD_MODES else None,
                "config_fingerprint": config_fingerprint,
                "config": config,
                "status": "queued",
                "created_at": timestamp,
                "updated_at": timestamp,
                "started_at": None,
                "completed_at": None,
                "pause_requested": False,
                "quota_paused": False,
                "quota_note": "",
                "current_range_id": None,
                "error": "",
                "ranges": ranges,
            }
            _finish_range_job(job)
            manifest["jobs"][job_id] = job
            self._write_range_manifest(manifest)

        if self.background and job["status"] == "queued":
            if production:
                try:
                    self._launch_worker(job_id, range_job=True)
                except Exception as exc:
                    self._mutate_range_job(job_id, lambda value: _mark_range_job_failed(
                        value, _redact(exc, self.config)))
            else:
                self._launch_background_range_worker(job_id)
        return self.range_snapshot(job_id) or _range_snapshot(job)

    def run_range_pending(self, job_id: str) -> dict:
        """Run waiting range entries through their persisted execution backend."""
        job = self.range_snapshot(job_id)
        if job is None:
            raise ValueError("Range job was not found.")
        if self.extractor is None and job.get("execution_kind", "synthetic") != "production":
            raise RuntimeError("Live range execution is disabled; an explicitly injected synthetic extractor is required.")
        if job.get("execution_kind") == "production" and _fixture_mode():
            raise RuntimeError("Fixture mode blocks live production range extraction.")
        if job.get("mode") in _CLOUD_MODES and not _has_range_consent(job):
            raise ValueError("Persisted cloud consent is missing or invalid; the range job cannot run.")
        if job.get("execution_kind") == "production":
            self.config = self._production_config(job["mode"], job.get("config", {}))
        try:
            self._assert_range_runtime_config(job)
        except ValueError as exc:
            self._mutate_range_job(job_id, lambda value: _mark_range_job_failed(value, str(exc)))
            raise
        with _file_lock(self.root / "locks" / f"range-job-{job_id}.lock", blocking=False):
            with self._range_worker_lease(job_id) as acquired:
                if not acquired:
                    return self.range_snapshot(job_id) or job
                self._mutate_range_job(job_id, _recover_interrupted_ranges)
                self._mutate_range_job(job_id, _begin_range_job)
                while True:
                    current = self.range_snapshot(job_id)
                    if current is None:
                        raise RuntimeError("Range job disappeared while it was running.")
                    if current.get("quota_paused"):
                        break
                    if current.get("pause_requested"):
                        self._mutate_range_job(job_id, _mark_range_job_paused)
                        break
                    item = next((value for value in current["ranges"] if value["status"] == "waiting"), None)
                    if item is None:
                        break
                    range_id = item["range_id"]
                    attempt_started = _now()
                    self._mutate_range_job(job_id, lambda value: _begin_range(value, range_id, attempt_started))
                    try:
                        verified = BatchStore(self.output_dir).validate_page_range(current["batch_id"], range_id)
                        _assert_range_matches_job(item, verified)
                        cache_context = _range_cache_context(
                            verified, current["batch_id"], current["mode"],
                            item["config_fingerprint"], _range_extractor_is_versioned(current["config"]),
                            job_id)
                        cache_context["execution_kind"] = current.get("execution_kind", "synthetic")
                        cached = self._find_range_cache(item["cache_identity"], cache_context)
                        if cached:
                            outcome = cached["status"]
                            cache_path, result_count = cached["cache_path"], cached["result_count"]
                        else:
                            context = {
                                **verified,
                                "batch_id": current["batch_id"],
                                "job_id": job_id,
                                "mode": current["mode"],
                                "config_fingerprint": item["config_fingerprint"],
                            }
                            if current.get("execution_kind") == "production":
                                from production_range_extractor import extract_range
                                router = self._range_router(current)
                                before_request = lambda model, tokens=0: self._reserve_range_provider_request(
                                    job_id, model, router.ledger if router is not None else None, tokens)
                                result = extract_range(
                                    context, current["mode"], self._range_runtime_config(current),
                                    self.output_dir, router=router,
                                    before_provider_request=before_request,
                                )
                            else:
                                result = self.extractor(context, current["mode"], dict(self.config or {}))
                            rows = _normalize_range_results(result)
                            if _range_quota_result(rows):
                                outcome, cache_path, result_count = "quota_paused", "", len(rows)
                            elif _range_failure_result(rows):
                                message = _range_failure_note(rows)
                                self._mutate_range_job(job_id, lambda value: _fail_range(
                                    value, range_id, "failed", message, attempt_started))
                                continue
                            else:
                                result_count = len(rows)
                                outcome = "no_recipe" if _range_no_recipe(rows) else "completed"
                                cache_path = self._write_range_cache(item, current, rows, outcome)
                        self._mutate_range_job(job_id, lambda value: _finish_range(
                            value, range_id, outcome, cache_path, result_count, attempt_started))
                    except Exception as exc:
                        message = _redact(exc, self.config)
                        outcome = "quota_paused" if _is_quota_error(message) else "failed"
                        self._mutate_range_job(job_id, lambda value: _fail_range(
                            value, range_id, outcome, message, attempt_started))
                    latest = self.range_snapshot(job_id)
                    if latest and latest.get("quota_paused"):
                        break
                self._mutate_range_job(job_id, _finish_range_job)
        finished = self.range_snapshot(job_id) or {}
        if finished.get("status") in {"completed", "completed_with_errors"}:
            self.register_completed_ranges(job_id)
        return finished

    def pause_range_job(self, job_id: str) -> dict:
        def pause(job: dict) -> None:
            if job.get("status") in {"completed", "completed_with_errors", "failed"}:
                return
            job["pause_requested"] = True
            processing = any(item.get("status") == "processing" for item in job.get("ranges", []))
            job["status"] = "pausing" if processing else "paused"
        self._mutate_range_job(job_id, pause)
        return self.range_snapshot(job_id) or {}

    def resume_range_job(self, job_id: str) -> dict:
        job = self.range_snapshot(job_id)
        if job is None:
            raise ValueError("Range job was not found.")
        if (job.get("status") == "completed"
                and all(self._find_job_range_cache(item, job)
                        for item in job.get("ranges", []))):
            return job
        if self.extractor is None and job.get("execution_kind", "synthetic") != "production":
            raise RuntimeError("Live range execution is disabled; an explicitly injected synthetic extractor is required.")
        if job.get("mode") in _CLOUD_MODES and not _has_range_consent(job):
            raise ValueError("Persisted cloud consent is missing or invalid; the range job cannot resume.")
        if job.get("execution_kind") == "production":
            self.config = self._production_config(job["mode"], job.get("config", {}))
        self._assert_range_runtime_config(job)
        try:
            with _file_lock(self.root / "locks" / f"range-job-{job_id}.lock", blocking=False):
                self._mutate_range_job(job_id, _recover_interrupted_ranges)
                self._mutate_range_job(job_id, self._prepare_range_resume)
        except _LockBusy:
            return self.range_snapshot(job_id) or job
        current = self.range_snapshot(job_id) or job
        if self.background and current.get("status") == "queued":
            if current.get("execution_kind") == "production":
                try:
                    self._launch_worker(job_id, range_job=True)
                except Exception as exc:
                    self._mutate_range_job(job_id, lambda value: _mark_range_job_failed(
                        value, _redact(exc, self.config)))
            else:
                self._launch_background_range_worker(job_id)
        return self.range_snapshot(job_id) or current

    def range_snapshot(self, job_id: str) -> dict | None:
        with _file_lock(self.range_manifest_lock):
            job = self._read_range_manifest()["jobs"].get(str(job_id))
            return _range_snapshot(job) if job is not None else None

    def register_completed_ranges(self, job_id: str) -> list[dict]:
        """Register every successful range in a completed job; safe to repeat after restart."""
        job = self.range_snapshot(str(job_id))
        if job is None or job.get("status") not in {"completed", "completed_with_errors"}:
            raise ValueError("Only finished range jobs can be registered for review.")
        registrations = []
        for item in job.get("ranges", []):
            if item.get("status") != "completed":
                continue
            registrations.extend(self.register_completed_range(
                str(job["batch_id"]), str(item["parent_document_id"]),
                str(item["range_id"]), job_id=str(job_id),
            ))
        return registrations

    def register_completed_range(self, batch_id: str, parent_document_id: str,
                                 range_id: str, *, job_id: str) -> list[dict]:
        """Register only a completed synthetic result bound to its verified original range."""
        job = self.range_snapshot(str(job_id))
        if (job is None or job.get("batch_id") != str(batch_id)
                or job.get("status") not in {"completed", "completed_with_errors"}):
            raise ValueError("Only a completed range job can be registered for review.")
        item = next((value for value in job.get("ranges", [])
                     if value.get("range_id") == str(range_id)), None)
        if item is None or item.get("status") != "completed":
            if item is not None and item.get("status") == "no_recipe":
                return []
            raise ValueError("The selected range has no completed recipe result.")
        store = BatchStore(self.output_dir)
        verified = store.validate_page_range(str(batch_id), str(range_id))
        _assert_range_matches_job(item, verified)
        versioned = _range_extractor_is_versioned(job.get("config", {}))
        expected = _range_cache_context(
            verified, str(batch_id), str(job["mode"]), str(item["config_fingerprint"]),
            versioned, str(job_id),
        )
        expected["execution_kind"] = job.get("execution_kind", "synthetic")
        if item.get("cache_identity") != expected["cache_identity"]:
            raise ValueError("The completed range cache identity is contradictory.")
        cache = self._find_range_cache(expected["cache_identity"], expected)
        if cache is None or cache.get("status") != "completed":
            raise ValueError("The completed range extraction cache is missing or invalid.")
        cache_path = Path(cache["cache_path"])
        record = json.loads(cache_path.read_text(encoding="utf-8"))
        cache_job_id = str(record.get("job_id", ""))
        cache_job = self.range_snapshot(cache_job_id) if cache_job_id else None
        cache_job_item = next((value for value in (cache_job or {}).get("ranges", [])
                               if value.get("range_id") == str(range_id)), None)
        if (cache_job is None or cache_job.get("status") not in {"completed", "completed_with_errors"}
                or cache_job_item is None or cache_job_item.get("status") != "completed"
                or cache_job_item.get("cache_identity") != expected["cache_identity"]):
            raise ValueError("The extraction cache is not bound to a completed range job.")
        batch = store.get(str(batch_id))
        parent = next((value for value in batch.get("parent_documents", [])
                       if value.get("document_id") == str(parent_document_id)), None) if batch else None
        if (parent is None or parent.get("sha256") != verified.get("parent_sha256")
                or parent.get("document_id") != verified.get("parent_document_id")):
            raise ValueError("The selected parent cookbook does not match the verified range.")

        from recipe_quality import ExtractionBatch
        from review_store import ReviewStore

        candidate_rows = []
        for result in record["results"]:
            row = result.get("recipe", result.get("candidate", result)) if isinstance(result, dict) else None
            if not isinstance(row, dict) or str(row.get("status", "")).upper() in _FAILED_RESULT_STATES | _NO_RECIPE_STATES:
                raise ValueError("The completed range contains an invalid or unsuccessful recipe result.")
            candidate_rows.append(row)
        extraction = ExtractionBatch.model_validate({"recipes": candidate_rows})
        if not extraction.recipes:
            return []
        provenance = {
            "schema_version": 1, "kind": "cookbook_range_v1",
            "batch_id": str(batch_id), "parent_document_id": verified["parent_document_id"],
            "parent_sha256": verified["parent_sha256"],
            "parent_filename": str(parent.get("original_filename", "")),
            "batch_name": str(batch.get("name", "")),
            "batch_tag": str(batch.get("tag", "")),
            "range_id": verified["range_id"], "first_page": verified["first_page"],
            "last_page": verified["last_page"], "page_count": verified["page_count"],
            "input_path": verified["input_path"], "input_sha256": verified["input_sha256"],
            "mode": job["mode"], "extraction_method": job["mode"],
            "config_fingerprint": item["config_fingerprint"],
            "cache_identity": item["cache_identity"], "cache_path": cache["cache_path"],
            "cache_record_sha256": str(record.get("record_sha256", "")),
            "extractor_version": str(job.get("config", {}).get("extractor_version") or "unversioned"),
            "job_id": cache_job_id, "source_path": verified["parent_path"],
        }
        return ReviewStore(self.output_dir / "review_events.jsonl").register_range_candidates(
            verified["parent_path"], verified["parent_sha256"], provenance, extraction.recipes,
        )

    def list_range_jobs(self) -> list[dict]:
        with _file_lock(self.range_manifest_lock):
            jobs = list(self._read_range_manifest()["jobs"].values())
        return sorted((_range_snapshot(item) for item in jobs),
                      key=lambda item: (item.get("created_at", ""), item["job_id"]), reverse=True)

    def _prepare_range_resume(self, job: dict) -> None:
        for item in job.get("ranges", []):
            if item.get("status") in {"failed", "quota_paused"}:
                item["status"] = "waiting"
                item["error"] = ""
            elif item.get("status") in {"completed", "no_recipe"}:
                cached = self._find_job_range_cache(item, job)
                if not cached:
                    item["status"] = "waiting"
                    item["error"] = "The durable range result cache is missing or corrupt; this explicit resume will retry the range."
                else:
                    item["cache_path"] = cached["cache_path"]
        job["pause_requested"] = False
        job["quota_paused"] = False
        job["quota_note"] = ""
        job["completed_at"] = None
        _finish_range_job(job)

    def _assert_range_runtime_config(self, job: dict) -> None:
        production = job.get("execution_kind") == "production"
        extractor = _production_range_marker if production else self.extractor
        config = _range_config_snapshot(self.config or {}, extractor,
                                        None if production else self._unversioned_range_scope)
        canonical = json.dumps(config, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        fingerprint = sha256(canonical.encode("utf-8")).hexdigest()
        if fingerprint != job.get("config_fingerprint"):
            raise ValueError("Range job configuration fingerprint does not match the persisted job.")

    def _range_runtime_config(self, job: dict) -> dict:
        if job.get("execution_kind") == "production" and self.config is None:
            self.config = self._production_config(job["mode"], job.get("config", {}))
        return dict(self.config or {})

    def _production_config(self, mode: str, persisted: Mapping[str, Any] | None = None) -> dict:
        config = self._runtime_config(mode)
        for key, value in (persisted or {}).items():
            if not any(part in str(key).casefold()
                       for part in ("key", "token", "secret", "password", "credential")):
                config[key] = value
        import import_recipes
        config.setdefault("extractor_version", "range-production-v1")
        config.setdefault("prompt_version", import_recipes.OCR_STRUCTURE_PROMPT_VERSION
                           if mode == OCR_ASSISTED else import_recipes.PROMPT_VERSION)
        config.setdefault("ocr_implementation", "local_ocr.extract_local")
        config.setdefault("ocr_version", import_recipes.OCR_TEXT_VERSION)
        config.setdefault("recipe_schema_version", "ExtractionBatch-v1")
        return config

    def _range_router(self, job: dict):
        if job.get("mode") not in _CLOUD_MODES:
            return None
        from model_routing import ModelRouter, UsageLedger, parse_model_config
        config = self._range_runtime_config(job)
        return ModelRouter(
            parse_model_config(config.get("models_json"), str(config.get("model", ""))),
            UsageLedger(self.output_dir / "gemini_usage.json"),
            global_rpm=float(config.get("global_rpm", 12)),
        )

    def _assert_range_cloud_consent(self, job_id: str, model: str) -> None:
        job = self.range_snapshot(job_id)
        if job is None or not _has_range_consent(job):
            raise ValueError("Persisted cloud consent is missing or invalid; provider request blocked.")

    def _reserve_range_provider_request(self, job_id: str, model_id: str, ledger=None,
                                        request_tokens: int = 0) -> None:
        self._assert_range_cloud_consent(job_id, model_id)
        from model_routing import parse_model_config
        job = self.range_snapshot(job_id)
        config = self._range_runtime_config(job)
        models = parse_model_config(config.get("models_json"), str(config.get("model", "")))
        model = next((item for item in models if item.id == model_id and item.enabled), None)
        if model is None:
            raise ValueError("The selected Gemini model is not present in the effective range configuration.")
        if ledger is None:
            from model_routing import UsageLedger
            ledger = UsageLedger(self.output_dir / "gemini_usage.json")
        ledger.reserve_scheduled(
            model.id, model.rpd, model_rpm=model.rpm,
            global_rpm=float(config.get("global_rpm", 12)), tpm=model.tpm,
            tokens=request_tokens,
            sleep=time.sleep,
        )

    def _read_range_manifest(self) -> dict:
        if not self.range_manifest_path.exists():
            return {"schema_version": 1, "jobs": {}}
        try:
            data = json.loads(self.range_manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError("Range job history is unavailable; existing range data was preserved.") from exc
        if not isinstance(data, dict) or not isinstance(data.get("jobs"), dict):
            raise RuntimeError("Range job history has an unsupported shape.")
        return data

    def _write_range_manifest(self, data: dict) -> None:
        _atomic_json(self.range_manifest_path, data)

    def _mutate_range_job(self, job_id: str, operation: Callable[[dict], None]) -> dict:
        with _file_lock(self.range_manifest_lock):
            manifest = self._read_range_manifest()
            job = manifest["jobs"].get(str(job_id))
            if job is None:
                raise ValueError("Range job was not found.")
            operation(job)
            job["updated_at"] = _now()
            self._write_range_manifest(manifest)
            return _range_snapshot(job)

    def _find_job_range_cache(self, item: dict, job: dict) -> dict | None:
        verified = BatchStore(self.output_dir).validate_page_range(job["batch_id"], item["range_id"])
        _assert_range_matches_job(item, verified)
        versioned = _range_extractor_is_versioned(job.get("config", {}))
        expected = _range_cache_context(verified, job["batch_id"], job["mode"],
                                        item["config_fingerprint"], versioned, job["job_id"])
        expected["execution_kind"] = job.get("execution_kind", "synthetic")
        if item.get("cache_identity") != expected["cache_identity"]:
            return None
        return self._find_range_cache(expected["cache_identity"], expected)

    def _find_range_cache(self, cache_identity: str, expected: dict) -> dict | None:
        cache_dir = self._range_cache_dir(create=False)
        if cache_dir is None:
            return None
        for path in sorted(cache_dir.glob(f"{cache_identity}--*.json")):
            if path.is_symlink() or not path.is_file():
                continue
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
                payload = {key: value for key, value in record.items() if key != "record_sha256"}
                canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                owned_path = (path.resolve(strict=True).parent == cache_dir.resolve(strict=True)
                              and re.fullmatch(re.escape(cache_identity) + r"--[0-9a-f]{32}\.json", path.name) is not None)
                provenance_fields = (
                    "cache_identity", "batch_id", "parent_document_id", "parent_sha256", "range_id",
                    "first_page", "last_page", "input_sha256", "mode", "config_fingerprint",
                )
                provenance_matches = all(record.get(key) == expected.get(key) for key in provenance_fields)
                production = expected.get("execution_kind") == "production"
                execution_matches = record.get("execution_kind", "synthetic") == expected.get(
                    "execution_kind", "synthetic")
                if (type(record.get("schema_version")) is int
                        and record["schema_version"] == (2 if production else 1)
                        and record.get("kind") == ("production_range_result" if production else "synthetic_range_result")
                        and execution_matches
                        and record.get("cache_identity") == cache_identity
                        and provenance_matches and owned_path
                        and (expected.get("extractor_versioned")
                             or record.get("job_id") == expected.get("job_id"))
                        and record.get("record_sha256") == sha256(canonical.encode("utf-8")).hexdigest()
                        and isinstance(record.get("results"), list)):
                    status = record.get("status")
                    if status not in {"completed", "no_recipe"}:
                        continue
                    return {"cache_path": str(path), "result_count": len(record["results"]), "status": status}
            except (OSError, json.JSONDecodeError, AttributeError, TypeError, ValueError, RuntimeError):
                continue
        return None

    def _write_range_cache(self, item: dict, job: dict, results: list[dict], outcome: str) -> str:
        cache_dir = self._range_cache_dir(create=True)
        path = cache_dir / f"{item['cache_identity']}--{uuid.uuid4().hex}.json"
        payload = {
            "schema_version": 2 if job.get("execution_kind") == "production" else 1,
            "kind": "production_range_result" if job.get("execution_kind") == "production" else "synthetic_range_result",
            "execution_kind": job.get("execution_kind", "synthetic"),
            "cache_identity": item["cache_identity"],
            "batch_id": job["batch_id"],
            "parent_document_id": item["parent_document_id"],
            "parent_sha256": item["parent_sha256"],
            "range_id": item["range_id"],
            "first_page": item["first_page"],
            "last_page": item["last_page"],
            "input_sha256": item["input_sha256"],
            "mode": job["mode"],
            "config_fingerprint": item["config_fingerprint"],
            "job_id": job["job_id"],
            "status": outcome,
            "results": results,
            "created_at": _now(),
        }
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        payload["record_sha256"] = sha256(canonical.encode("utf-8")).hexdigest()
        _atomic_json(path, payload)
        return str(path)

    def _range_cache_dir(self, *, create: bool) -> Path | None:
        cache_dir = self.root / "range_cache"
        if create:
            cache_dir.mkdir(parents=True, exist_ok=True)
        if cache_dir.is_symlink() or not cache_dir.exists():
            if cache_dir.exists() or cache_dir.is_symlink():
                raise ValueError("Range cache storage path is unsafe.")
            return None
        try:
            resolved = cache_dir.resolve(strict=True)
            resolved.relative_to(self.output_dir)
        except (OSError, ValueError) as exc:
            raise ValueError("Range cache storage path is unsafe.") from exc
        if resolved != Path(os.path.abspath(cache_dir)):
            raise ValueError("Range cache storage path is redirected or unsafe.")
        return cache_dir

    @contextmanager
    def _range_worker_lease(self, job_id: str):
        while True:
            current = self.range_snapshot(job_id)
            if current is None or current.get("pause_requested"):
                yield False
                return
            lease = _file_lock(self.global_worker_lock, blocking=False)
            try:
                lease.__enter__()
                break
            except _LockBusy:
                self._mutate_range_job(job_id, _mark_range_waiting_for_worker)
                time.sleep(0.1)
        try:
            yield True
        finally:
            lease.__exit__(None, None, None)

    def _launch_background_range_worker(self, job_id: str) -> None:
        key = f"range:{job_id}"
        with _BACKGROUND_LOCK:
            existing = _BACKGROUND_THREADS.get(key)
            if existing is not None and existing.is_alive():
                return
            def run() -> None:
                try:
                    self.run_range_pending(job_id)
                except _LockBusy:
                    pass
                except Exception as exc:
                    try:
                        self._mutate_range_job(job_id, lambda value: _mark_range_job_failed(
                            value, _redact(exc, self.config)))
                    except Exception:
                        pass
                finally:
                    with _BACKGROUND_LOCK:
                        if _BACKGROUND_THREADS.get(key) is threading.current_thread():
                            _BACKGROUND_THREADS.pop(key, None)
            thread = threading.Thread(target=run, name=f"recipe-range-fixture-{job_id[:8]}", daemon=True)
            _BACKGROUND_THREADS[key] = thread
            thread.start()

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

    def _launch_worker(self, job_id: str, *, range_job: bool = False) -> None:
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
        if range_job:
            args.append("--range-job")
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


def _range_config_snapshot(config: Mapping[str, Any], extractor: Callable,
                           unversioned_scope: str | None = None) -> dict:
    safe = {}
    for key, value in config.items():
        name = str(key)
        if any(part in name.casefold() for part in ("key", "token", "secret", "password", "credential")):
            continue
        try:
            clean_value = _range_json_value(value)
            json.dumps(clean_value, ensure_ascii=False, sort_keys=True, allow_nan=False)
        except (TypeError, ValueError):
            raise ValueError("Range extraction config contains a value that cannot be safely fingerprinted.") from None
        safe[name] = clean_value
    safe.setdefault("extractor_version", str(getattr(extractor, "__range_extractor_version__", "")))
    if not _range_extractor_is_versioned(safe) and unversioned_scope:
        safe["unversioned_scope"] = unversioned_scope
    safe["extractor"] = f"{getattr(extractor, '__module__', '')}.{getattr(extractor, '__qualname__', type(extractor).__qualname__)}"
    code = getattr(extractor, "__code__", None)
    if code is None:
        call = getattr(type(extractor), "__call__", None)
        code = getattr(call, "__code__", None)
    if code is not None:
        safe["extractor_implementation_sha256"] = sha256(marshal.dumps(_normalized_code(code))).hexdigest()
    return safe


def _range_extractor_is_versioned(config: Mapping[str, Any]) -> bool:
    version = config.get("extractor_version")
    return isinstance(version, str) and bool(version.strip())


def _range_cache_context(item: dict, batch_id: str, mode: str, config_fingerprint: str,
                         versioned: bool, job_id: str) -> dict:
    identity = _range_cache_identity(item, str(batch_id), mode, config_fingerprint)
    return {
        "cache_identity": identity,
        "batch_id": str(batch_id),
        "parent_document_id": item["parent_document_id"],
        "parent_sha256": item["parent_sha256"],
        "range_id": item["range_id"],
        "first_page": item["first_page"],
        "last_page": item["last_page"],
        "input_sha256": item["input_sha256"],
        "mode": mode,
        "config_fingerprint": config_fingerprint,
        "extractor_versioned": versioned,
        "job_id": job_id,
    }


def _normalized_code(code: CodeType) -> CodeType:
    constants = tuple(_normalized_code(value) if isinstance(value, CodeType)
                      else tuple(_normalized_code(child) if isinstance(child, CodeType) else child
                                 for child in value) if isinstance(value, tuple) else value
                      for value in code.co_consts)
    return code.replace(co_consts=constants, co_filename="", co_firstlineno=1,
                        co_linetable=b"", co_exceptiontable=b"")


def _range_cache_identity(item: dict, batch_id: str, mode: str, config_fingerprint: str) -> str:
    identity = {
        "schema_version": 1,
        "batch_id": batch_id,
        "parent_sha256": item["parent_sha256"],
        "range_id": item["range_id"],
        "first_page": item["first_page"],
        "last_page": item["last_page"],
        "input_sha256": item["input_sha256"],
        "mode": mode,
        "config_fingerprint": config_fingerprint,
    }
    canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    return sha256(canonical.encode("utf-8")).hexdigest()


def _assert_range_matches_job(item: dict, verified: dict) -> None:
    fields = {
        "parent_document_id": "parent_document_id",
        "parent_sha256": "parent_sha256",
        "range_id": "range_id",
        "first_page": "first_page",
        "last_page": "last_page",
        "page_count": "page_count",
        "input_path": "input_path",
        "input_sha256": "input_sha256",
        "size_bytes": "size_bytes",
    }
    if any(item.get(key) != verified.get(value) for key, value in fields.items()):
        raise ValueError("The verified range input no longer matches the durable job identity.")


def _normalize_range_results(result: Any) -> list[dict]:
    if isinstance(result, Mapping):
        values = result.get("results", result.get("recipes"))
        if values is None:
            values = [result]
    else:
        values = getattr(result, "recipes", None)
        if values is None and hasattr(result, "model_dump"):
            dumped = result.model_dump(mode="json")
            values = dumped.get("results", dumped.get("recipes")) if isinstance(dumped, Mapping) else None
        if values is None:
            values = result
    if values is None:
        raise TypeError("Synthetic range extractor must return one or more result mappings.")
    if not isinstance(values, (list, tuple)):
        values = [values]
    rows = []
    for value in values:
        if hasattr(value, "model_dump"):
            value = value.model_dump(mode="json")
        if not isinstance(value, Mapping):
            raise TypeError("Synthetic range results must be mappings or JSON model objects.")
        row = _range_json_value(dict(value))
        if not isinstance(row, dict):
            raise TypeError("Synthetic range results must be JSON objects.")
        for key in ("candidate_id", "candidate_ids", "source_path", "source_hash"):
            row.pop(key, None)
        rows.append(row)
    return rows


def _range_json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _range_json_value(item) for key, item in value.items()
                if not any(part in str(key).casefold() for part in ("key", "token", "secret", "password", "credential"))}
    if isinstance(value, (list, tuple)):
        return [_range_json_value(item) for item in value]
    raise TypeError("Synthetic range results must contain JSON-compatible values.")


def _production_range_marker(context: dict, mode: str, config: dict) -> list[dict]:
    """Stable fingerprint marker; production dispatch is implemented by the adapter."""
    raise RuntimeError("Production range extraction must run through the durable range worker.")


def _has_range_consent(job: dict) -> bool:
    if job.get("mode") not in _CLOUD_MODES:
        return True
    if job.get("cloud_consent") is not True:
        return False
    try:
        consent_at = datetime.fromisoformat(str(job.get("cloud_consent_at") or ""))
    except ValueError:
        return False
    return consent_at.tzinfo is not None


def _range_quota_result(rows: list[dict]) -> bool:
    return any(_is_quota_result(row) for row in rows)


def _range_failure_result(rows: list[dict]) -> bool:
    return any(str(row.get("status", "")).upper() in _FAILED_RESULT_STATES for row in rows)


def _range_failure_note(rows: list[dict]) -> str:
    return next((str(row.get("note") or row.get("failure_reason") or "Range extraction failed.")[:500]
                 for row in rows if str(row.get("status", "")).upper() in _FAILED_RESULT_STATES),
                "Range extraction failed.")


def _is_quota_error(message: str) -> bool:
    text = str(message).casefold()
    return any(term in text for term in ("quota", "resource_exhausted", "429",
                                         "daily request limit", "tpm allowance", "rate limit"))


def _range_no_recipe(rows: list[dict]) -> bool:
    return not rows or all(str(row.get("status", "")).upper() in _NO_RECIPE_STATES for row in rows)


def _range_snapshot(job: dict) -> dict:
    value = json.loads(json.dumps(job))
    terminal = {"completed", "no_recipe", "failed", "quota_paused"}
    completed = sum(1 for item in value.get("ranges", []) if item.get("status") in terminal)
    value["progress"] = {
        "total": len(value.get("ranges", [])),
        "completed": completed,
        "remaining": max(0, len(value.get("ranges", [])) - completed),
    }
    value["ranges"] = sorted(value.get("ranges", []), key=lambda item: (
        item.get("parent_document_id", ""), item.get("first_page", 0), item.get("last_page", 0),
    ))
    value["result_count"] = sum(int(item.get("result_count", 0)) for item in value["ranges"])
    value["failure_count"] = sum(item.get("status") == "failed" for item in value["ranges"])
    return value


def _find_range(job: dict, range_id: str) -> dict:
    item = next((value for value in job.get("ranges", []) if value.get("range_id") == range_id), None)
    if item is None:
        raise ValueError("Page range was not found in this job.")
    return item


def _begin_range_job(job: dict) -> None:
    if job.get("status") in {"completed", "completed_with_errors", "failed", "paused", "quota_paused"}:
        return
    job["status"] = "running"
    job["started_at"] = job.get("started_at") or _now()
    job["error"] = ""


def _mark_range_waiting_for_worker(job: dict) -> None:
    if job.get("status") == "queued" and not job.get("pause_requested"):
        job["status"] = "waiting_for_worker"


def _recover_interrupted_ranges(job: dict) -> None:
    for item in job.get("ranges", []):
        if item.get("status") != "processing":
            continue
        item["status"] = "waiting"
        item["error"] = "Previous range worker stopped before this range completed."
        attempts = item.get("attempts", [])
        if attempts and attempts[-1].get("status") == "processing":
            attempts[-1].update({"status": "interrupted", "completed_at": _now(), "error": item["error"]})
    job["current_range_id"] = None
    if job.get("status") in {"running", "pausing", "waiting_for_worker"}:
        job["status"] = "paused" if job.get("pause_requested") else "queued"


def _begin_range(job: dict, range_id: str, started_at: str) -> None:
    item = _find_range(job, range_id)
    item["status"] = "processing"
    item["error"] = ""
    item["attempt_count"] = int(item.get("attempt_count", 0)) + 1
    item["result_count"] = 0
    item["completed_at"] = None
    item.setdefault("attempts", []).append({
        "attempt": item["attempt_count"], "started_at": started_at, "status": "processing",
    })
    item["started_at"] = started_at
    job["current_range_id"] = range_id
    job["status"] = "running"


def _finish_range(job: dict, range_id: str, outcome: str, cache_path: str,
                  result_count: int, started_at: str) -> None:
    item = _find_range(job, range_id)
    item["status"] = outcome
    item["result_count"] = result_count
    if cache_path:
        item["cache_path"] = cache_path
    item["error"] = ""
    item["completed_at"] = _now()
    item["attempts"][-1].update({
        "status": outcome, "completed_at": item["completed_at"], "result_count": result_count,
    })
    job["current_range_id"] = None
    if outcome == "quota_paused":
        item["error"] = "Request allowance reached; resume when quota is available."
        job["quota_paused"] = True
        job["quota_note"] = item["error"]
        job["status"] = "quota_paused"
    elif job.get("pause_requested"):
        job["status"] = "paused"


def _fail_range(job: dict, range_id: str, outcome: str, error: str, started_at: str) -> None:
    item = _find_range(job, range_id)
    item["status"] = outcome
    item["error"] = error[:500]
    item["completed_at"] = _now()
    if item.get("attempts"):
        item["attempts"][-1].update({"status": outcome, "completed_at": item["completed_at"],
                                    "error": item["error"]})
    job["current_range_id"] = None
    if outcome == "quota_paused":
        job["quota_paused"] = True
        job["quota_note"] = item["error"]
        job["status"] = "quota_paused"
    elif job.get("pause_requested"):
        job["status"] = "paused"


def _mark_range_job_paused(job: dict) -> None:
    job["status"] = "paused"
    job["pause_requested"] = True
    job["current_range_id"] = None


def _mark_range_job_failed(job: dict, error: str) -> None:
    job["status"] = "failed"
    job["error"] = error[:500]
    job["completed_at"] = _now()


def _finish_range_job(job: dict) -> None:
    job["current_range_id"] = None
    statuses = [item.get("status") for item in job.get("ranges", [])]
    if job.get("quota_paused") or "quota_paused" in statuses:
        job["status"] = "quota_paused"
        job["quota_paused"] = True
        job["completed_at"] = None
    elif job.get("pause_requested"):
        job["status"] = "paused"
        job["completed_at"] = None
    elif "waiting" in statuses or "processing" in statuses:
        job["status"] = "queued"
        job["completed_at"] = None
    elif "failed" in statuses:
        job["status"] = "completed_with_errors"
        job["completed_at"] = _now()
    else:
        job["status"] = "completed"
        job["completed_at"] = _now()
    job["updated_at"] = _now()


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
