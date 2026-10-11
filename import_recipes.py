#!/usr/bin/env python3
"""Fast, resumable PDF/JPG -> Gemini -> Mealie v3.22+ bulk importer.

Assumes most files contain one recipe; can import multiple recipes from a single file.
No local OCR or Mealie-side AI is required.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import mimetypes
import os
import random
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
from pydantic import BaseModel, Field

SUPPORTED = {".pdf", ".jpg", ".jpeg", ".png", ".webp"}
INLINE_MAX_BYTES = 8 * 1024 * 1024  # Keep inline requests comfortably under 20MB.

PROMPT = """You are transcribing recipes from a scanned image or PDF into structured data.
Rules:
- Extract every distinct, reasonably complete recipe visible in this single source file.
- Preserve the original title, ingredient wording/quantities/units, and direction wording,
  order, temperatures, and times as faithfully as possible. Do not scale, correct,
  improve, invent, convert, or paraphrase a recipe.
- Return ingredients as one plain-text string per ingredient, including quantities.
- Return instructions as one plain-text string per original step or paragraph.
- Preserve recipe section headings in the adjacent ingredient/step string when relevant.
- If yield or description are not explicitly shown, use null.
- If something is illegible, DO NOT GUESS. Put a concise warning describing it in
  warnings, and transcribe only what is readable. Do not silently omit a necessary step.
- If the file has no usable recipe, return an empty recipes array.
- A recipe must not be invented from an advertisement, index, or photograph.
- Do not include explanatory prose outside the structured response.
"""


class RecipeCandidate(BaseModel):
    name: str
    ingredients: list[str] = Field(default_factory=list)
    instructions: list[str] = Field(default_factory=list)
    yield_text: Optional[str] = None
    description: Optional[str] = None
    warnings: list[str] = Field(default_factory=list)


class ExtractionBatch(BaseModel):
    recipes: list[RecipeCandidate] = Field(default_factory=list)
    general_warnings: list[str] = Field(default_factory=list)


class RateGate:
    """Thread-safe max call-start rate, shared by all worker threads."""
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
        remaining = start - now
        if remaining > 0:
            time.sleep(remaining)


class StateStore:
    def __init__(self, path: Path):
        self.path = path
        self.records: dict[str, dict] = {}
        self.lock = threading.Lock()
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    record = json.loads(line)
                    self.records[record["key"]] = record

    def get(self, key: str) -> Optional[dict]:
        with self.lock:
            return self.records.get(key)

    def add(self, key: str, status: str, slug: str, filename: str, title: str) -> None:
        record = {"key": key, "status": status, "slug": slug, "file": filename,
                  "title": title, "timestamp": datetime.now(timezone.utc).isoformat()}
        with self.lock:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            self.records[key] = record


_thread_local = threading.local()


def google_client(api_key: str):
    if not hasattr(_thread_local, "client"):
        _thread_local.client = genai.Client(api_key=api_key)
    return _thread_local.client


def file_digest(path: Path) -> str:
    hash_obj = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(1024 * 1024):
            hash_obj.update(chunk)
    return hash_obj.hexdigest()


def file_mime(path: Path) -> str:
    return {".pdf": "application/pdf", ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}[path.suffix.lower()]


def extract(path: Path, api_key: str, model: str, gate: RateGate) -> ExtractionBatch:
    client = google_client(api_key)
    mime = file_mime(path)
    upload = None
    try:
        if path.stat().st_size <= INLINE_MAX_BYTES:
            source = types.Part.from_bytes(data=path.read_bytes(), mime_type=mime)
        else:
            # Larger input: use Gemini's File API instead of a giant inline request.
            upload = client.files.upload(
                file=str(path), config=types.UploadFileConfig(mime_type=mime)
            )
            source = upload

        for attempt in range(5):
            try:
                gate.wait_turn()
                response = client.models.generate_content(
                    model=model,
                    contents=[PROMPT, source],
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_schema=ExtractionBatch,
                    ),
                )
                if not response.text:
                    raise ValueError("Gemini returned no text")
                return ExtractionBatch.model_validate_json(response.text)
            except Exception as exc:
                message = str(exc).lower()
                transient = any(term in message for term in (
                    "429", "resource_exhausted", "rate limit", "503", "502",
                    "504", "unavailable", "deadline exceeded", "timeout"
                ))
                if not transient or attempt == 4:
                    raise
                time.sleep(min(60.0, 3 * 2 ** attempt) + random.uniform(0, 1))
        raise RuntimeError("Gemini retry attempts exhausted")
    finally:
        if upload is not None and getattr(upload, "name", None):
            try:
                client.files.delete(name=upload.name)
            except Exception:
                pass  # Uploads expire; delete failure must not mask extraction.


def schema_recipe(recipe: RecipeCandidate) -> dict:
    """Build proper schema.org JSON-LD, not a Mealie internal recipe object."""
    data = {
        "@context": "https://schema.org",
        "@type": "Recipe",
        "name": recipe.name.strip(),
        "recipeIngredient": [x.strip() for x in recipe.ingredients if x.strip()],
        "recipeInstructions": [
            {"@type": "HowToStep", "text": x.strip()}
            for x in recipe.instructions if x.strip()
        ],
    }
    if recipe.yield_text:
        data["recipeYield"] = recipe.yield_text.strip()
    if recipe.description:
        data["description"] = recipe.description.strip()
    return data


def import_mealie(recipe: RecipeCandidate, mealie_url: str, token: str) -> str:
    url = mealie_url.rstrip("/") + "/api/recipes/create/html-or-json"
    payload = {
        "data": json.dumps(schema_recipe(recipe), ensure_ascii=False),
        "includeTags": False,
        "includeCategories": False,
    }
    response = requests.post(url, json=payload,
                             headers={"Authorization": f"Bearer {token}",
                                      "Accept": "application/json"},
                             timeout=(10, 100))
    if response.status_code != 201:
        raise RuntimeError(f"Mealie HTTP {response.status_code}: {response.text[:400]}")
    try:
        slug = response.json()
    except ValueError as exc:
        raise RuntimeError("Mealie returned HTTP 201 but no JSON slug") from exc
    if not isinstance(slug, str) or not slug.strip():
        raise RuntimeError(f"Mealie returned an unexpected slug: {slug!r}")
    return slug


def is_complete(recipe: RecipeCandidate) -> bool:
    return bool(recipe.name.strip() and
                any(x.strip() for x in recipe.ingredients) and
                any(x.strip() for x in recipe.instructions))


def process_file(path: Path, digest: str, config: dict, gate: RateGate,
                 state: StateStore, cache_dir: Path, extract_only: bool) -> list[dict]:
    def record(status: str, title: str = "", slug: str = "", note: str = "") -> dict:
        return {"file": str(path), "status": status, "title": title,
                "slug": slug, "note": note[:500]}

    cache_path = cache_dir / f"{digest}.json"
    try:
        if cache_path.exists():
            batch = ExtractionBatch.model_validate_json(cache_path.read_text(encoding="utf-8"))
        else:
            batch = extract(path, config["api_key"], config["model"], gate)
            tmp = cache_path.with_suffix(".tmp")
            tmp.write_text(batch.model_dump_json(indent=2), encoding="utf-8")
            os.replace(tmp, cache_path)
    except Exception as exc:
        return [record("EXTRACTION_FAILED", note=str(exc))]

    if not batch.recipes:
        return [record("NO_RECIPE", note="; ".join(batch.general_warnings))]

    results: list[dict] = []
    for idx, candidate in enumerate(batch.recipes):
        key = f"{digest}:{idx}"
        previous = state.get(key)
        warnings = "; ".join(batch.general_warnings + candidate.warnings)
        if previous and previous["status"] in {"IMPORTED", "UNCERTAIN"}:
            results.append(record("ALREADY_DONE" if previous["status"] == "IMPORTED" else
                                  "NEEDS_MANUAL_CHECK", candidate.name,
                                  previous.get("slug", ""), warnings))
            continue
        if not is_complete(candidate):
            results.append(record("INCOMPLETE", candidate.name, note=warnings or
                                  "Missing name, ingredients, or instructions"))
            continue
        if extract_only:
            results.append(record("EXTRACTED_ONLY", candidate.name, note=warnings))
            continue
        try:
            slug = import_mealie(candidate, config["mealie_url"], config["mealie_token"])
            state.add(key, "IMPORTED", slug, str(path), candidate.name)
            results.append(record("IMPORTED", candidate.name, slug, warnings))
        except Exception as exc:
            # 400/401/403/422 mean an explicit rejection and are safe to retry later.
            # A timeout/5xx/201-invalid-response may have created the recipe. Do not
            # automatically repost it on a rerun: duplicates are worse than review.
            error = str(exc)
            definite_reject = any(f"HTTP {code}:" in error for code in
                                  (400, 401, 403, 404, 409, 413, 415, 422, 429))
            if definite_reject:
                results.append(record("IMPORT_FAILED", candidate.name, note=error))
            else:
                state.add(key, "UNCERTAIN", "", str(path), candidate.name)
                results.append(record("NEEDS_MANUAL_CHECK", candidate.name,
                                      note=f"Import outcome unclear; inspect Mealie before retrying: {error}"))
    return results


def preflight_mealie(mealie_url: str, token: str) -> None:
    """Avoid spending Gemini quota when Mealie is offline or the token is invalid."""
    url = mealie_url.rstrip("/") + "/api/users/self"
    try:
        resp = requests.get(url, headers={"Authorization": f"Bearer {token}"},
                            timeout=(5, 15))
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise RuntimeError(f"Mealie preflight failed at {url}: {exc}") from exc


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Batch import PDF/JPG recipes into Mealie")
    parser.add_argument("--source", default=os.getenv("RECIPE_DIR", "recipes"),
                        help="Directory of PDF/JPG files (recursive)")
    parser.add_argument("--limit", type=int, default=0,
                        help="Only process N distinct source files (0 = all)")
    parser.add_argument("--extract-only", action="store_true",
                        help="Generate cached JSON but do not create Mealie recipes")
    parser.add_argument("--workers", type=int, default=int(os.getenv("WORKERS", "4")))
    parser.add_argument("--rpm", type=float, default=float(os.getenv("GEMINI_RPM", "12")))
    args = parser.parse_args()

    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    token = os.getenv("MEALIE_TOKEN", "").strip()
    url = os.getenv("MEALIE_URL", "").strip()
    model = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite").strip()
    missing = (["GEMINI_API_KEY"] if not api_key else [])
    if not args.extract_only:
        missing += (["MEALIE_TOKEN"] if not token else [])
        missing += (["MEALIE_URL"] if not url else [])
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

    work = Path(__file__).resolve().parent / "output"
    work.mkdir(exist_ok=True)
    cache_dir = work / "extracted"
    cache_dir.mkdir(exist_ok=True)
    state = StateStore(work / "import_state.jsonl")
    gate = RateGate(args.rpm)
    config = {"api_key": api_key, "mealie_token": token,
              "mealie_url": url, "model": model}
    unique: list[tuple[Path, str]] = []
    seen: set[str] = set()
    dupes = 0
    files = sorted((p for p in source.rglob("*") if p.is_file() and
                    p.suffix.lower() in SUPPORTED), key=lambda p: str(p).lower())
    print(f"Found {len(files)} candidate files. Hashing and removing exact duplicates...")
    for p in files:
        try:
            digest = file_digest(p)
        except OSError as exc:
            print(f"Cannot read {p}: {exc}", file=sys.stderr)
            continue
        if digest in seen:
            dupes += 1
            continue
        seen.add(digest)
        unique.append((p, digest))
        if args.limit and len(unique) >= args.limit:
            break
    print(f"Processing {len(unique)} unique files; {dupes} duplicate files skipped. "
          f"Model={model}, workers={args.workers}, Gemini RPM cap={args.rpm}.")

    rows = []
    try:
        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            futures = {executor.submit(process_file, p, digest, config, gate, state,
                                       cache_dir, args.extract_only): p
                       for p, digest in unique}
            for idx, future in enumerate(as_completed(futures), 1):
                try:
                    results = future.result()
                except Exception as exc:
                    results = [{"file": str(futures[future]), "status": "WORKER_FAILED",
                                "title": "", "slug": "", "note": str(exc)}]
                rows.extend(results)
                summary = ", ".join(f"{r['status']}: {r['title']}" for r in results)
                print(f"[{idx}/{len(unique)}] {futures[future].name}: {summary}", flush=True)
    except KeyboardInterrupt:
        print("Interrupted. Imported items already recorded; rerun to resume.", file=sys.stderr)
        return 130

    report = work / "last_run_report.csv"
    with report.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=["file", "status", "title", "slug", "note"])
        writer.writeheader()
        writer.writerows(rows)
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    print("\nRun summary:", counts)
    print(f"Report: {report}")
    print(f"JSON extraction cache: {cache_dir}")
    print(f"Persistent import log: {state.path}")
    return 1 if any(counts.get(x) for x in (
        "EXTRACTION_FAILED", "IMPORT_FAILED", "INCOMPLETE",
        "NEEDS_MANUAL_CHECK", "WORKER_FAILED")) else 0


if __name__ == "__main__":
    sys.exit(main())
