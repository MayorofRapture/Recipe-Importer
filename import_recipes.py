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
from review_store import ReviewStore, apply_review_decision

SUPPORTED = {".pdf", ".jpg", ".jpeg", ".png", ".webp"}
INLINE_MAX_BYTES = 8 * 1024 * 1024
PROMPT_VERSION = "quality-v1"

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


class AmbiguousCandidate(RuntimeError):
    pass


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
                if not response.text:
                    raise ValueError("Gemini returned no text")
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


def process_file(path: Path, digest: str, config: dict, gate: RateGate | None, state: StateStore, cache_dir: Path, extract_only: bool, retry_incomplete: bool = False, force_extract: bool = False, review_store: ReviewStore | None = None, router: ModelRouter | None = None, batch_tag: str = "", mealie_client=None) -> list[dict]:
    def record(status: str, title: str = "", slug: str = "", note: str = "", **extra) -> dict:
        return {"file": str(path), "status": status, "title": title, "slug": slug, "note": note[:500], **extra}
    legacy_cache = cache_dir / f"{digest}.json"
    selected_model = config.get("model", "")
    route_reason = "cache"
    try:
        if legacy_cache.exists() and not force_extract:
            batch = _load_cache(legacy_cache)
        elif router is not None:
            routed = router.run(lambda model: extract(path, config["api_key"], model.id, None), quality_check=_quality_issue)
            batch = routed.value
            selected_model = routed.model_id
            route_reason = routed.reason
            _save_cache(cache_dir, digest, selected_model, batch)
        else:
            batch = extract(path, config["api_key"], selected_model, gate)
            route_reason = "primary"
            if force_extract:
                _save_cache(cache_dir, digest, selected_model, batch)
            else:
                temp = legacy_cache.with_suffix(".tmp")
                temp.write_text(batch.model_dump_json(indent=2), encoding="utf-8")
                os.replace(temp, legacy_cache)
    except Exception as exc:
        return [record("EXTRACTION_FAILED", note=str(exc), model=selected_model, route_reason=route_reason)]
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
    if args.review_id:
        if not args.review_suggestion or not args.review_status:
            parser.error("--review-id requires --review-suggestion and --review-status")
        reviews = ReviewStore(work / "review_events.jsonl")
        event = reviews.candidates.get(args.review_id)
        if not event:
            parser.error(f"Review candidate not found: {args.review_id}")
        candidate = RecipeCandidate.model_validate(event["candidate"])
        try:
            reviews.record(event["source"], args.review_id, candidate, args.review_suggestion,
                           args.review_status, args.review_value)
        except ValueError as exc:
            parser.error(str(exc))
        reviews.write_csv(work / "needs_review.csv")
        print(f"Recorded {args.review_status} decision for {args.review_suggestion}.")
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
    config = {"api_key": api_key, "mealie_token": token, "mealie_url": url, "model": models[0].id}
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
        if args.limit and len(unique) >= args.limit:
            break
    print(f"Processing {len(unique)} unique files; models={','.join(item.id for item in models)}, workers={args.workers}.")
    rows = []
    try:
        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            futures = {executor.submit(process_file, path, digest, config, None, state, cache_dir,
                                        args.extract_only, args.retry_incomplete, args.force_extract,
                                        reviews, router, args.batch_tag, tag_client): path
                       for path, digest in unique}
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
