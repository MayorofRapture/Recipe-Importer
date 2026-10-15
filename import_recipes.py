#!/usr/bin/env python3
"""Resumable PDF/JPG -> Gemini -> Mealie importer."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import random
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests
from dotenv import load_dotenv
from google import genai
from google.genai import types

from model_routing import ModelRouter, UsageLedger, parse_model_config, validate_model_ids
from recipe_quality import ExtractionBatch, RecipeCandidate, is_complete, needs_review, schema_recipe
from mealie_client import MealieClient
import local_ocr
from ocr_structuring import OcrLineReferences, assemble_line_references, numbered_lines
from review_store import ReviewStore, apply_candidate_decision, apply_review_decision

SUPPORTED = {".pdf", ".jpg", ".jpeg", ".png", ".webp"}
INLINE_MAX_BYTES = 8 * 1024 * 1024
PROMPT_VERSION = "quality-v1"
OCR_TEXT_VERSION = "ocr-selected-v3"
OCR_ORIGINAL_TEXT_VERSION = "ocr-original-v3"
OCR_LAYOUT_TEXT_VERSION = "ocr-layout-v3"
OCR_STRUCTURE_CACHE_VERSION = 4
OCR_STRUCTURE_PROMPT_VERSION = "ocr-structure-v2-layout"

PROMPT = """You are transcribing recipes from a scanned image or PDF into structured data.
Authoritative fields must contain only text supported by the source. Preserve title,
ingredient wording/quantities/units, direction order, temperatures, and times; do not
scale, correct, improve, invent, convert, or paraphrase.
Return every distinct recipe as ingredients and instructions. If a necessary value is
visible but not extracted, add a `missing_information` finding with kind `not_extracted`.
If text is unreadable or cropped, say so with `unreadable` or `cropped`; if the source
simply does not print a value, use `missing_from_source`. Optional absent metadata such
as yield is not a critical finding.
For genuinely missing content, you may add a separate `suggestions` item containing a
clearly unverified plausible completion, rationale, confidence, uncertainty, and
pending review status. Never put a suggestion into ingredients or instructions.
If no usable recipe exists, return an empty recipes array. Do not include prose outside JSON.
"""

OCR_STRUCTURE_PROMPT = """You are locating recipe fields in numbered OCR text.
Return only JSON matching the response schema. Do not rewrite, correct, summarize,
translate, or invent any recipe content. Return one entry for each distinct recipe.
Use one-based inclusive line references into the supplied OCR text. A field may use
multiple noncontiguous ranges. Identify title, ingredients, and instructions/directions
separately. If a field is absent or uncertain, leave its ranges empty and explain the
uncertainty. Preserve source ordering and do not include headings unless they are part
of the field content. Confidence must describe the field-location confidence only.
"""


class AmbiguousCandidate(RuntimeError):
    pass


class GeminiExtractionError(RuntimeError):
    def __init__(self, message: str, *, model: str = "", diagnostics: dict | None = None,
                 attempted_models: list[str] | None = None, route_reason: str = ""):
        super().__init__(message)
        self.model = model
        self.diagnostics = diagnostics or {}
        self.attempted_models = attempted_models or ([model] if model else [])
        self.route_reason = route_reason
        self.stop_routing = self.diagnostics.get("reason") == "RECITATION"


class RateGate:
    def __init__(self, per_minute: float):
        if per_minute <= 0:
            raise ValueError("GEMINI_RPM must be greater than 0")
        self.delay = 60.0 / per_minute
        self.next_at = 0.0
        self.lock = threading.Lock()

    def wait_turn(self) -> None:
        with self.lock:
            now = time.monotonic()
            start = max(now, self.next_at)
            self.next_at = start + self.delay
        if start > now:
            time.sleep(start - now)


def _norm(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().casefold())


def candidate_identity(source_hash: str, candidate: RecipeCandidate) -> str:
    return _identity_for_title(source_hash, candidate.name)


def _identity_for_title(source_hash: str, title: str) -> str:
    title = _norm(title)
    return f"{source_hash}:{hashlib.sha256(title.encode('utf-8')).hexdigest()[:20]}"


class StateStore:
    def __init__(self, path: Path):
        self.path = path
        self.records: dict[str, dict] = {}
        self.lock = threading.Lock()
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    record = json.loads(line)
                    if "key" in record:
                        key_source, _, key_index = str(record["key"]).rpartition(":")
                        if not record.get("identity") and key_index.isdigit() and record.get("title"):
                            record["identity"] = _identity_for_title(key_source, str(record["title"]))
                        self.records[record["key"]] = record

    def get(self, key: str) -> Optional[dict]:
        with self.lock:
            return self.records.get(key)

    def add(self, key: str, status: str, slug: str, filename: str, title: str,
            identity: str = "", **fields) -> None:
        record = {"key": key, "status": status, "slug": slug, "file": filename,
                  "title": title, "timestamp": datetime.now(timezone.utc).isoformat()}
        if identity:
            record["identity"] = identity
        record.update(fields)
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            self.records[key] = record

    def find_candidate(self, source_hash: str, index: int,
                       candidate: RecipeCandidate) -> Optional[dict]:
        identity = candidate_identity(source_hash, candidate)
        legacy = self.records.get(f"{source_hash}:{index}")
        identity_matches = [record for record in self.records.values()
                            if record.get("identity") == identity]
        if len(identity_matches) > 1:
            raise AmbiguousCandidate(f"Multiple prior records match {candidate.name!r}; manual reconciliation required")
        if legacy and _norm(str(legacy.get("title", ""))) == _norm(candidate.name):
            return legacy
        matches = identity_matches
        if not matches:
            return None
        if len(matches) > 1:
            raise AmbiguousCandidate(f"Multiple prior records match {candidate.name!r}; manual reconciliation required")
        return matches[0]


_thread_local = threading.local()
_diagnostic_log_lock = threading.Lock()


def google_client(api_key: str):
    if not hasattr(_thread_local, "client"):
        _thread_local.client = genai.Client(api_key=api_key)
    return _thread_local.client


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def file_mime(path: Path) -> str:
    return {".pdf": "application/pdf", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".png": "image/png", ".webp": "image/webp"}[path.suffix.lower()]


def _jsonable(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if hasattr(value, "model_dump"):
        try:
            return _jsonable(value.model_dump(mode="json"))
        except TypeError:
            return _jsonable(value.model_dump())
    if hasattr(value, "to_dict"):
        try:
            return _jsonable(value.to_dict())
        except Exception:
            pass
    if hasattr(value, "__dict__"):
        return {key: _jsonable(item) for key, item in vars(value).items()
                if not key.startswith("_")}
    return str(value)


def _enum_name(value) -> str:
    if value is None:
        return ""
    name = getattr(value, "name", None)
    text = str(name or value)
    return text.rsplit(".", 1)[-1].upper()


def _response_diagnostics(response) -> dict:
    raw_candidates = getattr(response, "candidates", None)
    candidates = list(raw_candidates or [])
    finish_reasons = [_enum_name(getattr(item, "finish_reason", None))
                      for item in candidates]
    prompt_feedback = _jsonable(getattr(response, "prompt_feedback", None))
    safety_ratings = [_jsonable(getattr(item, "safety_ratings", []))
                      for item in candidates]
    return {
        "candidate_count": len(candidates),
        "finish_reason": finish_reasons[0] if finish_reasons else "",
        "finish_reasons": finish_reasons,
        "prompt_feedback": prompt_feedback,
        "safety_ratings": safety_ratings,
        "usage_metadata": _jsonable(getattr(response, "usage_metadata", None)),
    }


def _empty_response_reason(diagnostics: dict) -> str:
    finish_reasons = set(diagnostics.get("finish_reasons", []))
    prompt_feedback = diagnostics.get("prompt_feedback") or {}
    block_reason = _enum_name(prompt_feedback.get("block_reason")) \
        if isinstance(prompt_feedback, dict) else ""
    if "MAX_TOKENS" in finish_reasons:
        return "MAX_TOKENS"
    if "SAFETY" in finish_reasons or "SAFETY" in block_reason:
        return "SAFETY"
    if "RECITATION" in finish_reasons:
        return "RECITATION"
    if diagnostics.get("candidate_count", 0) == 0:
        return "MISSING_CANDIDATES"
    return "OTHER"


def extract(path: Path, api_key: str, model: str, gate: RateGate | None = None) -> ExtractionBatch:
    client = google_client(api_key)
    upload = None
    try:
        if path.stat().st_size <= INLINE_MAX_BYTES:
            source = types.Part.from_bytes(data=path.read_bytes(), mime_type=file_mime(path))
        else:
            upload = client.files.upload(file=str(path), config=types.UploadFileConfig(mime_type=file_mime(path)))
            source = upload
        for attempt in range(5):
            try:
                if gate is not None:
                    gate.wait_turn()
                response = client.models.generate_content(
                    model=model, contents=[PROMPT, source],
                    config=types.GenerateContentConfig(response_mime_type="application/json",
                                                       response_schema=ExtractionBatch),
                )
                if not getattr(response, "text", None):
                    diagnostics = _response_diagnostics(response)
                    diagnostics["reason"] = _empty_response_reason(diagnostics)
                    raise GeminiExtractionError(
                        f"Gemini returned no text ({diagnostics['reason']})",
                        model=model, diagnostics=diagnostics,
                    )
                return ExtractionBatch.model_validate_json(response.text)
            except Exception as exc:
                text = str(exc).lower()
                transient = any(term in text for term in ("429", "resource_exhausted", "rate limit", "503", "502", "504", "unavailable", "deadline exceeded", "timeout"))
                if not transient or attempt == 4:
                    raise
                time.sleep(min(60.0, 3 * 2 ** attempt) + random.uniform(0, 1))
        raise RuntimeError("Gemini retry attempts exhausted")
    finally:
        if upload is not None and getattr(upload, "name", None):
            try:
                client.files.delete(name=upload.name)
            except Exception:
                pass


def structure_ocr_text(text: str, api_key: str, model: str,
                       gate: RateGate | None = None) -> OcrLineReferences:
    """Ask Gemini for source line locations only; assemble recipe text locally."""
    client = google_client(api_key)
    for attempt in range(5):
        try:
            if gate is not None:
                gate.wait_turn()
            response = client.models.generate_content(
                model=model,
                contents=[OCR_STRUCTURE_PROMPT, numbered_lines(text)],
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=OcrLineReferences,
                ),
            )
            if not getattr(response, "text", None):
                diagnostics = _response_diagnostics(response)
                diagnostics["reason"] = _empty_response_reason(diagnostics)
                raise GeminiExtractionError(
                    f"Gemini text structuring returned no text ({diagnostics['reason']})",
                    model=model, diagnostics=diagnostics,
                )
            return OcrLineReferences.model_validate_json(response.text)
        except Exception as exc:
            text_lower = str(exc).lower()
            transient = any(term in text_lower for term in (
                "429", "resource_exhausted", "rate limit", "503", "502", "504",
                "unavailable", "deadline exceeded", "timeout",
            ))
            if not transient or attempt == 4:
                raise
            time.sleep(min(60.0, 3 * 2 ** attempt) + random.uniform(0, 1))
    raise RuntimeError("Gemini text structuring attempts exhausted")


def import_mealie(recipe: RecipeCandidate, mealie_url: str, token: str) -> str:
    url = mealie_url.rstrip("/") + "/api/recipes/create/html-or-json"
    response = requests.post(url, json={"data": json.dumps(schema_recipe(recipe), ensure_ascii=False), "includeTags": False, "includeCategories": False}, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"}, timeout=(10, 100))
    if response.status_code != 201:
        raise RuntimeError(f"Mealie HTTP {response.status_code}: {response.text[:400]}")
    try:
        slug = response.json()
    except ValueError as exc:
        raise RuntimeError("Mealie returned HTTP 201 but no JSON slug") from exc
    if not isinstance(slug, str) or not slug.strip():
        raise RuntimeError(f"Mealie returned an unexpected slug: {slug!r}")
    return slug


def _quality_issue(batch: ExtractionBatch) -> bool:
    return not batch.recipes or any(finding.kind in {"unreadable", "cropped", "ambiguous", "not_extracted"} for candidate in batch.recipes for finding in candidate.missing_information)


def _load_cache(path: Path) -> ExtractionBatch:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return ExtractionBatch.model_validate(raw["batch"] if isinstance(raw, dict) and "batch" in raw else raw)


def _save_cache(cache_dir: Path, digest: str, model: str, batch: ExtractionBatch) -> Path:
    safe_model = re.sub(r"[^A-Za-z0-9_.-]+", "_", model)
    path = cache_dir / f"{digest}--{safe_model}--{PROMPT_VERSION}--{int(time.time() * 1000)}.json"
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps({"schema_version": 2, "source_hash": digest, "model": model, "prompt_version": PROMPT_VERSION, "attempted_at": datetime.now(timezone.utc).isoformat(), "batch": batch.model_dump(mode="json")}, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(temp, path)
    return path


def _ocr_text_path(output_dir: Path, digest: str) -> Path:
    return output_dir / "ocr_text" / f"{digest}--{OCR_TEXT_VERSION}.txt"


def _ocr_text_paths(output_dir: Path, digest: str) -> dict[str, Path]:
    directory = output_dir / "ocr_text"
    return {
        "original": directory / f"{digest}--{OCR_ORIGINAL_TEXT_VERSION}.txt",
        "layout": directory / f"{digest}--{OCR_LAYOUT_TEXT_VERSION}.txt",
        "selected": _ocr_text_path(output_dir, digest),
    }


def _write_text_file(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(text, encoding="utf-8")
    os.replace(temp, path)


def _save_ocr_variants(output_dir: Path, digest: str,
                       result: local_ocr.OcrResult,
                       legacy_path: Path | None = None) -> dict[str, Path]:
    paths = _ocr_text_paths(output_dir, digest)
    values = {
        "original": result.original_text or result.text,
        "layout": result.layout_text or result.text,
        "selected": result.text,
    }
    for key, path in paths.items():
        _write_text_file(path, values[key])
    if legacy_path is not None:
        _write_text_file(legacy_path, result.text)
    return paths


def _save_ocr_structure_cache(cache_dir: Path, digest: str, method: str,
                              model: str, batch: ExtractionBatch,
                              ocr_metadata: dict | None = None) -> Path:
    safe_method = re.sub(r"[^A-Za-z0-9_.-]+", "_", method)
    safe_model = re.sub(r"[^A-Za-z0-9_.-]+", "_", model or "local")
    path = cache_dir / f"{digest}--ocr-structure--{safe_method}-{safe_model}--{int(time.time() * 1000)}.json"
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps({
        "schema_version": OCR_STRUCTURE_CACHE_VERSION,
        "source_hash": digest,
        "model": model,
        "prompt_version": OCR_STRUCTURE_PROMPT_VERSION,
        "structuring_method": method,
        "ocr_selected_variant": str((ocr_metadata or {}).get("selected_variant", "original")),
        "ocr_selection_reason": list((ocr_metadata or {}).get("uncertainties", [])),
        "attempted_at": datetime.now(timezone.utc).isoformat(),
        "batch": batch.model_dump(mode="json"),
    }, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(temp, path)
    return path


def _load_ocr_structure_cache(cache_dir: Path, digest: str) -> tuple[Path, ExtractionBatch, dict] | None:
    paths = list(cache_dir.glob(f"{digest}--ocr-structure--*.json"))
    def cache_key(item: Path) -> tuple[int, str]:
        match = re.search(r"--(\d+)\.json$", item.name)
        return (int(match.group(1)) if match else -1, item.name)

    paths.sort(key=cache_key, reverse=True)
    for path in paths:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if (not isinstance(raw, dict)
                    or raw.get("schema_version") != OCR_STRUCTURE_CACHE_VERSION
                    or raw.get("prompt_version") != OCR_STRUCTURE_PROMPT_VERSION):
                continue
            return path, _load_cache(path), raw if isinstance(raw, dict) else {}
        except (OSError, ValueError, json.JSONDecodeError):
            continue
    return None


def _redact_error(error: Exception, config: dict) -> str:
    text = str(error)
    for key in ("api_key", "mealie_token"):
        secret = str(config.get(key, ""))
        if secret:
            text = text.replace(secret, "[redacted]")
    return text[:2000]


def _csv_json(value) -> str:
    if value is None or value == "":
        return ""
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _write_gemini_diagnostic(log_path: Path, source: Path, digest: str,
                             model: str, route_reason: str, attempted_models: list[str],
                             error: Exception, config: dict,
                             ocr_info: dict | None = None) -> None:
    diagnostics = getattr(error, "diagnostics", None) or {
        "reason": "OTHER", "error_type": type(error).__name__,
    }
    event = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "file": str(source),
        "source_hash": digest,
        "model": model,
        "route_reason": route_reason,
        "attempted_models": attempted_models,
        "error_type": type(error).__name__,
        "error": _redact_error(error, config),
        "diagnostics": diagnostics,
    }
    if ocr_info:
        event.update(ocr_info)
    try:
        with _diagnostic_log_lock:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, ensure_ascii=False) + "\n")
                fh.flush()
    except OSError:
        pass


def _diagnostic_report_fields(error: Exception, model: str, attempted_models: list[str],
                              log_path: Path, ocr_info: dict | None = None) -> dict:
    diagnostics = getattr(error, "diagnostics", None) or {}
    fields = {
        "diagnostic_reason": diagnostics.get("reason", "OTHER"),
        "finish_reason": diagnostics.get("finish_reason", ""),
        "finish_reasons": _csv_json(diagnostics.get("finish_reasons")),
        "candidate_count": diagnostics.get("candidate_count", ""),
        "prompt_feedback": _csv_json(diagnostics.get("prompt_feedback")),
        "safety_ratings": _csv_json(diagnostics.get("safety_ratings")),
        "usage_metadata": _csv_json(diagnostics.get("usage_metadata")),
        "models_attempted": _csv_json(attempted_models),
        "diagnostic_log": str(log_path),
    }
    if ocr_info:
        fields.update(ocr_info)
    return fields


def _save_ocr_text(output_dir: Path, digest: str, text: str) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{digest}--{int(time.time() * 1000)}.txt"
    temp = path.with_suffix(".tmp")
    temp.write_text(text, encoding="utf-8")
    os.replace(temp, path)
    return path


def _ocr_report_fields(original_error: Exception, ocr_status: str, *,
                       engine: str = "", text_path: Path | None = None,
                       note: str = "") -> dict:
    diagnostics = getattr(original_error, "diagnostics", None) or {}
    return {
        "original_gemini_reason": diagnostics.get("reason", "RECITATION"),
        "ocr_attempted": True,
        "ocr_engine": engine,
        "ocr_status": ocr_status,
        "ocr_text_path": str(text_path) if text_path else "",
        "manual_review_required": True,
        "ocr_note": note,
    }


RETRYABLE_STATUSES = {
    "INCOMPLETE", "NEEDS_REVIEW", "FAILED", "EXTRACTION_FAILED", "IMPORT_FAILED",
    "TAG_FAILED", "WORKER_FAILED",
}


def _path_key(path: Path | str) -> str:
    try:
        return os.path.normcase(str(Path(path).resolve()))
    except OSError:
        return os.path.normcase(str(path))


def _load_retry_report(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        with path.open(encoding="utf-8-sig", newline="") as fh:
            return list(csv.DictReader(fh))
    except (OSError, csv.Error):
        return []


def _cache_paths(cache_dir: Path, digest: str) -> list[Path]:
    paths = []
    legacy = cache_dir / f"{digest}.json"
    if legacy.exists():
        paths.append(legacy)
    paths.extend(cache_dir.glob(f"{digest}--*.json"))

    def cache_key(path: Path) -> tuple[int, int, int, str]:
        if path.name == f"{digest}.json":
            return (0, 0, 0, path.name)
        match = re.search(r"--(\d+)\.json$", path.name)
        if match:
            return (1, int(match.group(1)), 0, path.name)
        try:
            return (1, 0, path.stat().st_mtime_ns, path.name)
        except OSError:
            return (1, 0, 0, path.name)

    return sorted(paths, key=cache_key, reverse=True)


def _latest_cache(cache_dir: Path, digest: str) -> Path | None:
    paths = _cache_paths(cache_dir, digest)
    return paths[0] if paths else None


def _load_latest_cache(cache_dir: Path, digest: str) -> ExtractionBatch | None:
    for path in _cache_paths(cache_dir, digest):
        try:
            return _load_cache(path)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
    return None


def _source_has_retryable_state(state: StateStore, path: Path, digest: str) -> bool:
    source_key = _path_key(path)
    for record in state.records.values():
        same_digest = str(record.get("key", "")).startswith(f"{digest}:")
        same_file = record.get("file") and _path_key(record["file"]) == source_key
        if (same_digest or same_file) and record.get("status") in RETRYABLE_STATUSES:
            return True
    return False


def _source_has_retryable_cache(state: StateStore, path: Path, digest: str,
                                cache_dir: Path) -> bool:
    cache_paths = _cache_paths(cache_dir, digest)
    if not cache_paths:
        return False
    batch = _load_latest_cache(cache_dir, digest)
    if batch is None:
        return True
    for index, candidate in enumerate(batch.recipes):
        try:
            previous = state.find_candidate(digest, index, candidate)
        except AmbiguousCandidate:
            return True
        if previous and previous.get("status") in {"IMPORTED", "UNCERTAIN"}:
            continue
        if not is_complete(candidate) or needs_review(candidate):
            return True
    return False


def retry_notice(path: Path, models: list[str] | list[object]) -> str:
    model_ids = [item.id if hasattr(item, "id") else str(item) for item in models]
    primary = model_ids[0] if model_ids else "(none configured)"
    candidates = ",".join(model_ids)
    return f"Retrying {path} with model={primary} (candidates={candidates})"


def select_sources(files: list[tuple[Path, str]], state: StateStore, cache_dir: Path,
                   report_path: Path, retry_incomplete: bool, limit: int) -> list[tuple[Path, str]]:
    """Select source files without letting --limit hide eligible retry work."""
    if not retry_incomplete:
        return files[:limit] if limit else files
    report = _load_retry_report(report_path)
    report_by_file: dict[str, set[str]] = {}
    for row in report:
        file_name = row.get("file", "")
        status = row.get("status", "")
        if file_name and status in RETRYABLE_STATUSES:
            report_by_file.setdefault(_path_key(file_name), set()).add(status)
    eligible = []
    for path, digest in files:
        if (_source_has_retryable_state(state, path, digest)
                or _source_has_retryable_cache(state, path, digest, cache_dir)
                or _path_key(path) in report_by_file):
            eligible.append((path, digest))
    return eligible[:limit] if limit else eligible


def process_file(path: Path, digest: str, config: dict, gate: RateGate | None, state: StateStore, cache_dir: Path, extract_only: bool, retry_incomplete: bool = False, force_extract: bool = False, review_store: ReviewStore | None = None, router: ModelRouter | None = None, batch_tag: str = "", mealie_client=None) -> list[dict]:
    report_fields: dict = {}

    def record(status: str, title: str = "", slug: str = "", note: str = "", **extra) -> dict:
        return {"file": str(path), "status": status, "title": title, "slug": slug,
                "note": note[:500], **report_fields, **extra}
    selected_model = config.get("model", "")
    route_reason = "cache"
    try:
        legacy_cache = cache_dir / f"{digest}.json"
        cache_paths = [] if force_extract else _cache_paths(cache_dir, digest)
        cached_batch = _load_latest_cache(cache_dir, digest) if cache_paths else None
        if cache_paths and cached_batch is None:
            raise ValueError(f"No valid extraction cache found for {digest}")
        if cached_batch is not None:
            batch = cached_batch
        elif router is not None:
            route_reason = "primary"
            routed = router.run(lambda model: extract(path, config["api_key"], model.id, None), quality_check=_quality_issue)
            batch = routed.value
            selected_model = routed.model_id
            route_reason = routed.reason
            _save_cache(cache_dir, digest, selected_model, batch)
        else:
            route_reason = "primary"
            batch = extract(path, config["api_key"], selected_model, gate)
            if force_extract:
                _save_cache(cache_dir, digest, selected_model, batch)
            else:
                temp = legacy_cache.with_suffix(".tmp")
                temp.write_text(batch.model_dump_json(indent=2), encoding="utf-8")
                os.replace(temp, legacy_cache)
    except Exception as exc:
        selected_model = getattr(exc, "model", "") or selected_model
        route_reason = getattr(exc, "route_reason", "") or route_reason
        attempted_models = getattr(exc, "attempted_models", None) or ([selected_model] if selected_model else [])
        diagnostic_log = cache_dir.parent / "gemini_diagnostics.jsonl"
        diagnostics = getattr(exc, "diagnostics", None) or {}
        if diagnostics.get("reason") == "RECITATION":
            original_route_reason = route_reason
            ocr_path = _save_ocr_text(cache_dir.parent / "ocr_text", digest, "")
            report_fields.update(_ocr_report_fields(
                exc, "started", text_path=ocr_path,
                note="Gemini RECITATION; attempting local OCR."))
            report_fields["original_gemini_route_reason"] = original_route_reason
            try:
                ocr_result = local_ocr.extract_local(
                    path, config.get("tesseract_cmd", os.getenv("TESSERACT_CMD", "")))
                ocr_path.write_text(ocr_result.text, encoding="utf-8")
                report_fields.update(_ocr_report_fields(
                    exc, "success", engine=ocr_result.engine, text_path=ocr_path,
                    note=ocr_result.note))
                batch = ocr_result.batch
                route_reason = "local_ocr_recitation"
                _write_gemini_diagnostic(
                    diagnostic_log, path, digest, selected_model, original_route_reason,
                    attempted_models, exc, config, report_fields)
                report_fields.update(_diagnostic_report_fields(
                    exc, selected_model, attempted_models, diagnostic_log,
                    report_fields))
            except Exception as ocr_exc:
                report_fields.update(_ocr_report_fields(
                    exc, "failed", text_path=ocr_path,
                    note=_redact_error(ocr_exc, config)))
                _write_gemini_diagnostic(
                    diagnostic_log, path, digest, selected_model, original_route_reason,
                    attempted_models, exc, config, report_fields)
                return [record(
                    "NEEDS_REVIEW", note=(f"Gemini RECITATION; local OCR failed: "
                                           f"{_redact_error(ocr_exc, config)}"),
                    model=selected_model, route_reason=route_reason,
                    **_diagnostic_report_fields(exc, selected_model,
                                                attempted_models, diagnostic_log,
                                                report_fields))]
        else:
            _write_gemini_diagnostic(diagnostic_log, path, digest, selected_model,
                                     route_reason, attempted_models, exc, config)
            return [record("EXTRACTION_FAILED", note=_redact_error(exc, config),
                           model=selected_model, route_reason=route_reason,
                           **_diagnostic_report_fields(exc, selected_model,
                                                       attempted_models, diagnostic_log))]
    if not batch.recipes:
        return [record("NO_RECIPE", note="; ".join(batch.general_warnings), model=selected_model, route_reason=route_reason)]
    reviews = review_store or ReviewStore(cache_dir.parent / "review_events.jsonl")
    results: list[dict] = []
    for index, candidate in enumerate(batch.recipes):
        identity = candidate_identity(digest, candidate)
        try:
            previous = state.find_candidate(digest, index, candidate)
        except AmbiguousCandidate as exc:
            reviews.add_candidate(str(path), identity, candidate)
            results.append(record("NEEDS_REVIEW", candidate.name, note=str(exc), model=selected_model, route_reason=route_reason, candidate_id=identity))
            continue
        decisions = reviews.decisions_for(identity)
        if decisions:
            candidate = apply_review_decision(candidate, decisions)
            approved_fields = {item.field_path for item in candidate.suggestions
                               if item.review_status in {"approved", "edited"}}
            candidate.missing_information = [item for item in candidate.missing_information
                                             if item.field_path not in approved_fields]
        candidate = apply_candidate_decision(candidate, reviews.candidate_decision(identity))
        warnings = "; ".join(batch.general_warnings + candidate.warnings)
        if previous and previous.get("status") == "IMPORTED" and batch_tag \
                and previous.get("tag_status") == "failed" \
                and previous.get("tag_name") == batch_tag:
            try:
                tag = mealie_client.ensure_tag(batch_tag)
                tag_status = mealie_client.add_tag(previous.get("slug", ""), tag)
                state.add(identity, "IMPORTED", previous.get("slug", ""), str(path), candidate.name,
                          identity=identity, model=selected_model, route_reason=route_reason,
                          tag_status=tag_status, tag_name=batch_tag)
                results.append(record("TAGGED_REPAIRED", candidate.name, previous.get("slug", ""),
                                      model=selected_model, route_reason=route_reason, candidate_id=identity))
            except Exception as exc:
                state.add(identity, "IMPORTED", previous.get("slug", ""), str(path), candidate.name,
                          identity=identity, model=selected_model, route_reason=route_reason,
                          tag_status="failed", tag_name=batch_tag, tag_note=str(exc))
                results.append(record("TAG_FAILED", candidate.name, previous.get("slug", ""), str(exc),
                                      model=selected_model, route_reason=route_reason, candidate_id=identity))
            continue
        if previous and previous.get("status") in {"IMPORTED", "UNCERTAIN"}:
            results.append(record("ALREADY_DONE" if previous["status"] == "IMPORTED" else "NEEDS_MANUAL_CHECK", candidate.name, previous.get("slug", ""), warnings, model=selected_model, route_reason=route_reason, candidate_id=identity))
            continue
        if candidate.missing_information or candidate.suggestions:
            reviews.add_candidate(str(path), identity, candidate)
        if not is_complete(candidate):
            results.append(record("NEEDS_REVIEW" if candidate.missing_information or candidate.suggestions else "INCOMPLETE", candidate.name, note=warnings or "Missing name, ingredients, or instructions", model=selected_model, route_reason=route_reason, candidate_id=identity))
            continue
        if needs_review(candidate):
            results.append(record("NEEDS_REVIEW", candidate.name, note=warnings or "Critical source detail requires review", model=selected_model, route_reason=route_reason, candidate_id=identity))
            continue
        if extract_only:
            results.append(record("EXTRACTED_ONLY", candidate.name, note=warnings, model=selected_model, route_reason=route_reason, candidate_id=identity))
            continue
        try:
            slug = import_mealie(candidate, config["mealie_url"], config["mealie_token"])
            tag_status = ""
            if batch_tag:
                try:
                    if mealie_client is None:
                        raise RuntimeError("Batch tag requested but no Mealie tag client is configured")
                    tag_status = mealie_client.add_tag(slug, mealie_client.ensure_tag(batch_tag))
                except Exception as tag_exc:
                    state.add(identity, "IMPORTED", slug, str(path), candidate.name, identity=identity,
                              model=selected_model, route_reason=route_reason, tag_status="failed", tag_name=batch_tag,
                              tag_note=str(tag_exc))
                    results.append(record("TAG_FAILED", candidate.name, slug, str(tag_exc),
                                          model=selected_model, route_reason=route_reason, candidate_id=identity))
                    continue
            state.add(identity, "IMPORTED", slug, str(path), candidate.name, identity=identity,
                      model=selected_model, route_reason=route_reason, tag_status=tag_status)
            results.append(record("IMPORTED", candidate.name, slug, warnings,
                                  model=selected_model, route_reason=route_reason, candidate_id=identity))
        except Exception as exc:
            error = str(exc)
            definite_reject = any(f"HTTP {code}:" in error for code in (400, 401, 403, 404, 409, 413, 415, 422, 429))
            if definite_reject:
                results.append(record("IMPORT_FAILED", candidate.name, note=error, model=selected_model, route_reason=route_reason, candidate_id=identity))
            else:
                state.add(identity, "UNCERTAIN", "", str(path), candidate.name, identity=identity, model=selected_model, route_reason=route_reason)
                results.append(record("NEEDS_MANUAL_CHECK", candidate.name, note=f"Import outcome unclear; inspect Mealie before retrying: {error}", model=selected_model, route_reason=route_reason, candidate_id=identity))
    return results


def _ocr_only_file(path: Path, digest: str, output_dir: Path,
                   tesseract_cmd: str, reviews: ReviewStore) -> list[dict]:
    legacy_path = output_dir / "ocr_text" / f"{digest}.txt"
    variant_paths = _ocr_text_paths(output_dir, digest)
    text_path = variant_paths["selected"]
    try:
        if all(item.exists() and item.read_text(encoding="utf-8").strip()
               for item in variant_paths.values()):
            result = local_ocr.OcrResult.from_variants(
                variant_paths["original"].read_text(encoding="utf-8"),
                variant_paths["layout"].read_text(encoding="utf-8"),
                engine="cached-ocr")
        elif legacy_path.exists() and legacy_path.read_text(encoding="utf-8").strip():
            result = local_ocr.OcrResult.from_text(
                legacy_path.read_text(encoding="utf-8"), engine="cached-ocr")
            _save_ocr_variants(output_dir, digest, result, legacy_path)
        else:
            result = local_ocr.extract_local(path, tesseract_cmd)
            _save_ocr_variants(output_dir, digest, result, legacy_path)
    except Exception as exc:
        return [{
            "file": str(path), "status": "OCR_FAILED", "title": "", "slug": "",
            "note": _redact_error(exc, {"api_key": "", "mealie_token": ""})[:500],
            "ocr_attempted": True, "ocr_engine": "", "ocr_status": "failed",
            "ocr_text_path": str(text_path) if text_path.exists() else "",
            "ocr_original_text_path": str(variant_paths["original"])
            if variant_paths["original"].exists() else "",
            "ocr_layout_text_path": str(variant_paths["layout"])
            if variant_paths["layout"].exists() else "",
            "ocr_selected_text_path": str(text_path) if text_path.exists() else "",
            "manual_review_required": True, "route_reason": "local_ocr_only",
        }]

    if not result.batch.recipes:
        return [{
            "file": str(path), "status": "OCR_NO_RECIPE", "title": "", "slug": "",
            "note": "OCR returned text but no conservative recipe section was detected.",
            "ocr_attempted": True, "ocr_engine": result.engine, "ocr_status": "success",
            "ocr_text_path": str(text_path),
            "ocr_original_text_path": str(variant_paths["original"])
            if variant_paths["original"].exists() else "",
            "ocr_layout_text_path": str(variant_paths["layout"])
            if variant_paths["layout"].exists() else "",
            "ocr_selected_text_path": str(text_path),
            "ocr_selected_variant": result.selected_variant,
            "ocr_selection_reason": "; ".join(result.uncertainties),
            "manual_review_required": True,
            "route_reason": "local_ocr_only",
        }]

    rows = []
    for index, candidate in enumerate(result.batch.recipes):
        identity = candidate_identity(digest, candidate)
        if identity not in reviews.candidates:
            reviews.add_candidate(str(path), identity, candidate)
        note = "; ".join(result.batch.general_warnings + candidate.warnings + [
            finding.detail for finding in candidate.missing_information])
        rows.append({
            "file": str(path), "status": "OCR_NEEDS_REVIEW", "title": candidate.name,
            "slug": "", "note": note[:500], "model": "",
            "route_reason": "local_ocr_only", "candidate_id": identity,
            "ocr_attempted": True, "ocr_engine": result.engine,
            "ocr_status": "success", "ocr_text_path": str(text_path),
            "ocr_original_text_path": str(variant_paths["original"]),
            "ocr_layout_text_path": str(variant_paths["layout"]),
            "ocr_selected_text_path": str(text_path),
            "ocr_selected_variant": result.selected_variant,
            "ocr_selection_reason": "; ".join(result.uncertainties),
            "manual_review_required": True,
        })
    return rows


def _run_ocr_only(source: Path, output_dir: Path, *, limit: int = 0,
                  workers: int = 1, tesseract_cmd: str = "") -> int:
    if workers < 1 or limit < 0:
        raise ValueError("workers must be > 0 and limit must be >= 0")
    output_dir.mkdir(parents=True, exist_ok=True)
    reviews = ReviewStore(output_dir / "review_events.jsonl")
    unique: list[tuple[Path, str]] = []
    seen: set[str] = set()
    for path in sorted((p for p in source.rglob("*")
                        if p.is_file() and p.suffix.lower() in SUPPORTED),
                       key=lambda p: str(p).lower()):
        try:
            digest = file_digest(path)
        except OSError:
            continue
        if digest in seen:
            continue
        seen.add(digest)
        unique.append((path, digest))
    selected = unique[:limit] if limit else unique
    print(f"Processing {len(selected)} unique files with local OCR only; workers={workers}.")
    rows: list[dict] = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(_ocr_only_file, path, digest, output_dir,
                            tesseract_cmd, reviews): path
            for path, digest in selected
        }
        for future in as_completed(futures):
            try:
                rows.extend(future.result())
            except Exception as exc:
                rows.append({"file": str(futures[future]), "status": "OCR_FAILED",
                             "title": "", "slug": "", "note": str(exc),
                             "ocr_attempted": True, "ocr_status": "failed",
                             "manual_review_required": True,
                             "route_reason": "local_ocr_only"})
    report = output_dir / "last_run_report.csv"
    fields = sorted({key for row in rows for key in row}) if rows else [
        "file", "status", "title", "slug", "note"]
    with report.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    reviews.write_csv(output_dir / "needs_review.csv")
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    print("\nOCR-only summary:", counts)
    print(f"Report: {report}")
    return 1 if any(status in counts for status in
                    ("OCR_NEEDS_REVIEW", "OCR_FAILED", "OCR_NO_RECIPE")) else 0


def _prepare_python_ocr_batch(result: local_ocr.OcrResult) -> ExtractionBatch:
    batch = result.batch.model_copy(deep=True)
    generic_detail = "OCR transcription and recipe boundaries require human verification before import."
    for candidate in batch.recipes:
        if result.structured and is_complete(candidate):
            candidate.missing_information = [
                finding for finding in candidate.missing_information
                if finding.detail != generic_detail
            ]
        candidate.source_method = "python"
        candidate.confidence = result.confidence
        candidate.uncertainties = list(result.uncertainties)
        candidate.requires_review = True
    return batch


def _write_run_report(output_dir: Path, rows: list[dict]) -> Path:
    report = output_dir / "last_run_report.csv"
    fields = sorted({key for row in rows for key in row}) if rows else [
        "file", "status", "title", "slug", "note"]
    with report.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return report


def _ocr_structure_file(path: Path, digest: str, output_dir: Path, config: dict,
                        router: ModelRouter, reviews: ReviewStore,
                        force_extract: bool = False) -> list[dict]:
    cache_dir = output_dir / "extracted"
    cache_dir.mkdir(parents=True, exist_ok=True)
    variant_paths = _ocr_text_paths(output_dir, digest)
    text_path = variant_paths["selected"]
    cache_entry = None if force_extract else _load_ocr_structure_cache(cache_dir, digest)
    method = ""
    model = ""
    route_reason = ""
    cache_path = ""
    ocr_engine = "cached-ocr"
    selected_variant = "cache"
    selection_reason: list[str] = []
    local_result = None
    try:
        if cache_entry is not None:
            cached_path, batch, metadata = cache_entry
            cache_path = str(cached_path)
            method = str(metadata.get("structuring_method", "")) or (
                batch.recipes[0].source_method if batch.recipes else "gemini_text")
            model = str(metadata.get("model", ""))
            route_reason = "ocr_structure_cache"
            selected_variant = str(metadata.get("ocr_selected_variant", "cache"))
            selection_reason = list(metadata.get("ocr_selection_reason", []))
        else:
            if all(item.exists() and item.read_text(encoding="utf-8").strip()
                   for item in variant_paths.values()):
                local_result = local_ocr.OcrResult.from_variants(
                    variant_paths["original"].read_text(encoding="utf-8"),
                    variant_paths["layout"].read_text(encoding="utf-8"),
                    engine="cached-ocr")
            elif text_path.exists() and text_path.read_text(encoding="utf-8").strip():
                text = text_path.read_text(encoding="utf-8")
                local_result = local_ocr.OcrResult.from_text(text, engine="cached-ocr")
            else:
                local_result = local_ocr.extract_local(
                    path, config.get("tesseract_cmd", os.getenv("TESSERACT_CMD", "")))
                _save_ocr_variants(output_dir, digest, local_result)
            ocr_engine = local_result.engine
            selected_variant = local_result.selected_variant
            selection_reason = list(local_result.uncertainties)
            if local_result.structured:
                batch = _prepare_python_ocr_batch(local_result)
                method = "python"
                route_reason = "local_ocr_python"
            else:
                routed = router.run(
                    lambda routed_model: structure_ocr_text(
                        local_result.text, config["api_key"], routed_model.id, None)
                )
                batch = assemble_line_references(local_result.text, routed.value)
                method = "gemini_text"
                model = routed.model_id
                route_reason = routed.reason
            cache_path = str(_save_ocr_structure_cache(
                cache_dir, digest, method, model, batch,
                {"selected_variant": selected_variant,
                 "uncertainties": selection_reason}))

        results = []
        for candidate in batch.recipes:
            identity = candidate_identity(digest, candidate)
            if identity not in reviews.candidates:
                reviews.add_candidate(str(path), identity, candidate)
            decision = reviews.candidate_decision(identity)
            review_status = decision.status if decision else "pending"
            status = "OCR_STRUCTURED_APPROVED" if review_status in {"approved", "edited"} \
                else "OCR_STRUCTURED_NEEDS_REVIEW"
            results.append({
                "file": str(path), "status": status, "title": candidate.name,
                "slug": "", "note": "; ".join(batch.general_warnings),
                "model": model, "route_reason": route_reason,
                "candidate_id": identity, "ocr_engine": ocr_engine,
                "ocr_status": "success", "ocr_text_path": str(text_path)
                if text_path.exists() else "", "structuring_method": method,
                "ocr_original_text_path": str(variant_paths["original"])
                if variant_paths["original"].exists() else "",
                "ocr_layout_text_path": str(variant_paths["layout"])
                if variant_paths["layout"].exists() else "",
                "ocr_selected_text_path": str(text_path)
                if text_path.exists() else "",
                "ocr_selected_variant": selected_variant,
                "ocr_selection_reason": _csv_json(selection_reason),
                "gemini_model": model, "recipe_count": len(batch.recipes),
                "confidence": candidate.confidence,
                "uncertainties": _csv_json(candidate.uncertainties),
                "review_status": review_status,
                "structured_cache_path": cache_path,
                "manual_review_required": True,
            })
        return results
    except Exception as exc:
        if local_result is not None:
            for candidate in local_result.batch.recipes:
                identity = candidate_identity(digest, candidate)
                if identity not in reviews.candidates:
                    reviews.add_candidate(str(path), identity, candidate)
        diagnostics = getattr(exc, "diagnostics", None) or {}
        attempted_models = getattr(exc, "attempted_models", None) or (
            [getattr(exc, "model", "")] if getattr(exc, "model", "") else [])
        diagnostic_log = output_dir / "gemini_diagnostics.jsonl"
        if isinstance(exc, GeminiExtractionError):
            _write_gemini_diagnostic(
                diagnostic_log, path, digest, getattr(exc, "model", "") or model,
                getattr(exc, "route_reason", "") or route_reason,
                attempted_models, exc, config,
                {"ocr_attempted": True, "ocr_engine": ocr_engine,
                 "ocr_status": "success", "ocr_text_path": str(text_path)
                 if text_path.exists() else "", "manual_review_required": True,
                 "ocr_original_text_path": str(variant_paths["original"])
                 if variant_paths["original"].exists() else "",
                 "ocr_layout_text_path": str(variant_paths["layout"])
                 if variant_paths["layout"].exists() else "",
                 "structuring_method": "gemini_text"},
            )
        reason = diagnostics.get("reason", "STRUCTURING_FAILED")
        return [{
            "file": str(path), "status": "OCR_STRUCTURE_FAILED", "title": "",
            "slug": "", "note": _redact_error(exc, config),
            "model": getattr(exc, "model", "") or model,
            "route_reason": getattr(exc, "route_reason", "") or route_reason,
            "ocr_engine": ocr_engine, "ocr_status": "success"
            if text_path.exists() else "failed",
            "ocr_text_path": str(text_path) if text_path.exists() else "",
            "ocr_original_text_path": str(variant_paths["original"])
            if variant_paths["original"].exists() else "",
            "ocr_layout_text_path": str(variant_paths["layout"])
            if variant_paths["layout"].exists() else "",
            "ocr_selected_text_path": str(text_path) if text_path.exists() else "",
            "ocr_selected_variant": selected_variant,
            "ocr_selection_reason": _csv_json(selection_reason),
            "structuring_method": "gemini_text" if method != "python" else method,
            "failure_reason": reason, "diagnostic_log": str(diagnostic_log)
            if isinstance(exc, GeminiExtractionError) else "",
            "manual_review_required": True,
        }]


def _run_ocr_structure(source: Path, output_dir: Path, config: dict,
                       router: ModelRouter, *, limit: int = 0, workers: int = 1,
                       force_extract: bool = False) -> int:
    if workers < 1 or limit < 0:
        raise ValueError("workers must be > 0 and limit must be >= 0")
    output_dir.mkdir(parents=True, exist_ok=True)
    reviews = ReviewStore(output_dir / "review_events.jsonl")
    unique: list[tuple[Path, str]] = []
    seen: set[str] = set()
    for path in sorted((p for p in source.rglob("*")
                        if p.is_file() and p.suffix.lower() in SUPPORTED),
                       key=lambda p: str(p).lower()):
        try:
            digest = file_digest(path)
        except OSError:
            continue
        if digest in seen:
            continue
        seen.add(digest)
        unique.append((path, digest))
    selected = unique[:limit] if limit else unique
    print(f"Processing {len(selected)} unique files with hybrid OCR structure; workers={workers}.")
    rows: list[dict] = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(_ocr_structure_file, path, digest, output_dir, config,
                            router, reviews, force_extract): path
            for path, digest in selected
        }
        for future in as_completed(futures):
            try:
                rows.extend(future.result())
            except Exception as exc:
                rows.append({"file": str(futures[future]), "status": "OCR_STRUCTURE_FAILED",
                             "title": "", "slug": "", "note": str(exc),
                             "manual_review_required": True})
    report = _write_run_report(output_dir, rows)
    reviews.write_csv(output_dir / "needs_review.csv")
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    print("\nOCR-structure summary:", counts)
    print(f"Report: {report}")
    return 1 if any(status in counts for status in (
        "OCR_STRUCTURED_NEEDS_REVIEW", "OCR_STRUCTURE_FAILED")) else 0


def preflight_mealie(mealie_url: str, token: str) -> None:
    url = mealie_url.rstrip("/") + "/api/users/self"
    try:
        resp = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=(5, 15))
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise RuntimeError(f"Mealie preflight failed at {url}: {exc}") from exc


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Batch import PDF/JPG recipes into Mealie")
    parser.add_argument("--source", default=os.getenv("RECIPE_DIR", "recipes"))
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--extract-only", action="store_true")
    parser.add_argument("--ocr-only", action="store_true",
                        help="Use local OCR only; do not initialize Gemini or connect to Mealie")
    parser.add_argument("--ocr-structure", action="store_true",
                        help="Structure local OCR with Python first, then text-only Gemini if needed")
    parser.add_argument("--retry-incomplete", action="store_true")
    parser.add_argument("--force-extract", action="store_true")
    parser.add_argument("--workers", type=int, default=int(os.getenv("WORKERS", "4")))
    parser.add_argument("--rpm", type=float, default=float(os.getenv("GEMINI_RPM", "12")))
    parser.add_argument("--validate-models", action="store_true")
    parser.add_argument("--batch-tag", default=os.getenv("BATCH_TAG", ""),
                        help="Tag newly created recipes; existing recipes are not retagged")
    parser.add_argument("--backfill-tag", help="Explicitly tag only the slugs in --backfill-slugs")
    parser.add_argument("--backfill-slugs", help="Comma-separated Mealie slugs for --backfill-tag")
    parser.add_argument("--dry-run", action="store_true", help="Preview a scoped tag backfill")
    parser.add_argument("--review-id")
    parser.add_argument("--review-suggestion")
    parser.add_argument("--review-status", choices=["pending", "approved", "edited", "rejected"])
    parser.add_argument("--review-value")
    args = parser.parse_args()
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    token = os.getenv("MEALIE_TOKEN", "").strip()
    url = os.getenv("MEALIE_URL", "").strip()
    model = os.getenv("GEMINI_MODEL", "").strip()
    work = Path(__file__).resolve().parent / "output"
    work.mkdir(exist_ok=True)
    if args.ocr_only and args.ocr_structure:
        parser.error("--ocr-only and --ocr-structure are mutually exclusive")
    if args.ocr_only:
        incompatible = []
        if args.extract_only:
            incompatible.append("--extract-only")
        if args.retry_incomplete:
            incompatible.append("--retry-incomplete")
        if args.force_extract:
            incompatible.append("--force-extract")
        if args.validate_models:
            incompatible.append("--validate-models")
        if args.batch_tag or args.backfill_tag or args.backfill_slugs or args.dry_run:
            incompatible.append("tag/backfill options")
        if args.review_id or args.review_suggestion or args.review_status or args.review_value:
            incompatible.append("review decision options")
        if incompatible:
            parser.error("--ocr-only cannot be combined with " + ", ".join(incompatible))
        if args.workers < 1 or args.limit < 0:
            parser.error("--workers must be > 0; --limit must be >= 0")
        source = Path(args.source).expanduser()
        if not source.is_dir():
            parser.error(f"Recipe directory not found: {source}")
        return _run_ocr_only(
            source, work, limit=args.limit, workers=args.workers,
            tesseract_cmd=os.getenv("TESSERACT_CMD", "").strip())
    if args.ocr_structure:
        incompatible = []
        if args.retry_incomplete:
            incompatible.append("--retry-incomplete")
        if args.batch_tag or args.backfill_tag or args.backfill_slugs or args.dry_run:
            incompatible.append("tag/backfill options")
        if args.review_id or args.review_suggestion or args.review_status or args.review_value:
            incompatible.append("review decision options")
        if incompatible:
            parser.error("--ocr-structure cannot be combined with " + ", ".join(incompatible))
        if not api_key:
            parser.error("--ocr-structure requires GEMINI_API_KEY")
        if args.workers < 1 or args.rpm <= 0 or args.limit < 0:
            parser.error("--workers and --rpm must be > 0; --limit must be >= 0")
        source = Path(args.source).expanduser()
        if not source.is_dir():
            parser.error(f"Recipe directory not found: {source}")
        try:
            models = parse_model_config(os.getenv("GEMINI_MODELS_JSON"), model)
        except ValueError as exc:
            parser.error(str(exc))
        if args.validate_models:
            try:
                validate_model_ids(google_client(api_key), models)
            except Exception as exc:
                parser.error(f"Gemini model validation failed: {exc}")
        router = ModelRouter(models, UsageLedger(work / "gemini_usage.json"),
                             global_rpm=args.rpm)
        config = {"api_key": api_key, "model": models[0].id,
                  "tesseract_cmd": os.getenv("TESSERACT_CMD", "").strip()}
        return _run_ocr_structure(
            source, work, config, router, limit=args.limit, workers=args.workers,
            force_extract=args.force_extract)
    if args.review_id:
        if not args.review_status:
            parser.error("--review-id requires --review-status")
        reviews = ReviewStore(work / "review_events.jsonl")
        event = reviews.candidates.get(args.review_id)
        if not event:
            parser.error(f"Review candidate not found: {args.review_id}")
        try:
            if args.review_suggestion:
                candidate = RecipeCandidate.model_validate(event["candidate"])
                reviews.record(event["source"], args.review_id, candidate,
                               args.review_suggestion, args.review_status,
                               args.review_value)
                message = f"Recorded {args.review_status} decision for {args.review_suggestion}."
            else:
                reviews.record_candidate_decision(
                    event["source"], args.review_id, args.review_status,
                    args.review_value)
                message = f"Recorded {args.review_status} decision for candidate {args.review_id}."
        except ValueError as exc:
            parser.error(str(exc))
        reviews.write_csv(work / "needs_review.csv")
        print(message)
        return 0
    if args.backfill_tag:
        if not token or not url:
            parser.error("--backfill-tag requires MEALIE_TOKEN and MEALIE_URL")
        if not args.backfill_slugs or not any(item.strip() for item in args.backfill_slugs.split(",")):
            parser.error("--backfill-tag requires a non-empty --backfill-slugs list")
        result = MealieClient(url, token).backfill_tag(
            [item.strip() for item in args.backfill_slugs.split(",") if item.strip()],
            args.backfill_tag, dry_run=args.dry_run,
        )
        print("Tag backfill:", result)
        return 1 if any(value == "failed" for value in result.values()) else 0
    missing = ["GEMINI_API_KEY"] if not api_key else []
    if not args.extract_only:
        missing += [name for name, value in (("MEALIE_TOKEN", token), ("MEALIE_URL", url)) if not value]
    if missing:
        parser.error("Missing .env settings: " + ", ".join(missing))
    if args.workers < 1 or args.rpm <= 0 or args.limit < 0:
        parser.error("--workers and --rpm must be > 0; --limit must be >= 0")
    source = Path(args.source).expanduser()
    if not source.is_dir():
        parser.error(f"Recipe directory not found: {source}")
    if not args.extract_only:
        try:
            preflight_mealie(url, token)
            print("Mealie connection/token OK.")
        except RuntimeError as exc:
            parser.error(str(exc))
    try:
        models = parse_model_config(os.getenv("GEMINI_MODELS_JSON"), model)
    except ValueError as exc:
        parser.error(str(exc))
    if args.validate_models:
        try:
            validate_model_ids(google_client(api_key), models)
        except Exception as exc:
            parser.error(f"Gemini model validation failed: {exc}")
    cache_dir = work / "extracted"
    cache_dir.mkdir(exist_ok=True)
    state = StateStore(work / "import_state.jsonl")
    reviews = ReviewStore(work / "review_events.jsonl")
    router = ModelRouter(models, UsageLedger(work / "gemini_usage.json"), global_rpm=args.rpm)
    tag_client = MealieClient(url, token) if args.batch_tag and not args.extract_only else None
    config = {"api_key": api_key, "mealie_token": token, "mealie_url": url,
              "model": models[0].id, "tesseract_cmd": os.getenv("TESSERACT_CMD", "").strip()}
    unique: list[tuple[Path, str]] = []
    seen: set[str] = set()
    files = sorted((p for p in source.rglob("*") if p.is_file() and p.suffix.lower() in SUPPORTED), key=lambda p: str(p).lower())
    for path in files:
        try:
            digest = file_digest(path)
        except OSError as exc:
            print(f"Cannot read {path}: {exc}", file=sys.stderr)
            continue
        if digest in seen:
            continue
        seen.add(digest)
        unique.append((path, digest))
    selected = select_sources(unique, state, cache_dir, work / "last_run_report.csv",
                              args.retry_incomplete, args.limit)
    if args.retry_incomplete:
        for path, _ in selected:
            print(retry_notice(path, models), flush=True)
    print(f"Processing {len(selected)} unique files; models={','.join(item.id for item in models)}, workers={args.workers}.")
    rows = []
    try:
        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            futures = {executor.submit(process_file, path, digest, config, None, state, cache_dir,
                                        args.extract_only, args.retry_incomplete, args.force_extract,
                                        reviews, router, args.batch_tag, tag_client): path
                       for path, digest in selected}
            for future in as_completed(futures):
                try:
                    rows.extend(future.result())
                except Exception as exc:
                    rows.append({"file": str(futures[future]), "status": "WORKER_FAILED", "title": "", "slug": "", "note": str(exc)})
    except KeyboardInterrupt:
        print("Interrupted. Imported items already recorded; rerun to resume.", file=sys.stderr)
        return 130
    report = work / "last_run_report.csv"
    fields = sorted({key for row in rows for key in row}) if rows else ["file", "status", "title", "slug", "note"]
    with report.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    reviews.write_csv(work / "needs_review.csv")
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    print("\nRun summary:", counts)
    print(f"Report: {report}")
    return 1 if any(counts.get(x) for x in ("EXTRACTION_FAILED", "IMPORT_FAILED", "INCOMPLETE", "NEEDS_REVIEW", "NEEDS_MANUAL_CHECK", "WORKER_FAILED")) else 0


if __name__ == "__main__":
    sys.exit(main())
