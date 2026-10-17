"""Measure cold and warm synthetic catalog loading without touching user data."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import platform
import statistics
import sys
import tempfile
import time
import uuid
from io import BytesIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from batch_store import BatchStore
from import_recipes import file_digest
from PIL import Image
from recipe_quality import ExtractionBatch, RecipeCandidate
from review_store import ReviewStore, extraction_identity
from ui_data import load_catalog


def _prepopulate(root: Path, count: int, source_count: int,
                 *, prepopulate_registrations: bool = True) -> tuple[list[tuple[str, str, str, RecipeCandidate]], int]:
    sources = root / "sources"
    extracted = root / "extracted"
    sources.mkdir()
    extracted.mkdir()
    batch_store = BatchStore(root)
    batch_record = batch_store.create("Synthetic catalog benchmark")
    quotient, remainder = divmod(count, source_count)
    registrations = []
    events = []
    candidate_index = 0
    for source_index in range(source_count):
        source_path = sources / f"source-{source_index:04d}.jpg"
        image = Image.new("RGB", (2, 2), (source_index % 256, (source_index // 256) % 256, 127))
        image_bytes = BytesIO()
        image.save(image_bytes, format="JPEG")
        jpeg = image_bytes.getvalue()
        comment = f"synthetic-source-{source_index}".encode("ascii")
        comment_marker = b"\xff\xfe" + (len(comment) + 2).to_bytes(2, "big") + comment
        source_path.write_bytes(jpeg[:-2] + comment_marker + jpeg[-2:])
        staged = batch_store.stage(batch_record["batch_id"], source_path.name, source_path.read_bytes())
        if staged.get("status") != "staged":
            raise RuntimeError(f"could not prepare synthetic source: {staged}")
        source_hash = file_digest(source_path)
        assert staged["digest"] == source_hash
        source_candidates = quotient + (1 if source_index < remainder else 0)
        recipes = [RecipeCandidate(
            name=f"Synthetic source {source_index} recipe {offset}",
            ingredients=["1 cup synthetic rice"],
            instructions=["Cook the synthetic rice."],
        ) for offset in range(source_candidates)]
        batch = ExtractionBatch(recipes=recipes)
        extraction_id = extraction_identity(source_hash, batch.model_dump(mode="json"))
        (extracted / f"{source_hash}.json").write_text(json.dumps({
            "schema_version": 2, "source_hash": source_hash,
            "batch": batch.model_dump(mode="json"),
        }), encoding="utf-8")
        for recipe in recipes:
            payload = recipe.model_dump(mode="json")
            fingerprint = hashlib.sha256(json.dumps(
                payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            ).encode("utf-8")).hexdigest()
            candidate_id = str(uuid.uuid5(uuid.NAMESPACE_URL,
                                          f"catalog-benchmark:{source_hash}:{fingerprint}"))
            events.append({
                "event": "candidate_registration", "schema_version": 1,
                "event_id": f"benchmark-{candidate_id}",
                "operation_id": f"register:{extraction_id}:{fingerprint}:unique",
                "candidate_id": candidate_id, "source_hash": source_hash,
                "extraction_id": extraction_id, "timestamp": "2026-10-16T00:00:00+00:00",
                "actor": "synthetic-benchmark", "source": str(source_path),
                "fingerprint": fingerprint, "identity_status": "registered",
                "legacy_id": f"{source_hash}:{fingerprint[:24]}",
                "title_legacy_id": "", "legacy_aliases": [],
                "candidate": payload, "target_bindings": [],
            })
            registrations.append((candidate_id, source_hash, extraction_id, recipe))
            candidate_index += 1
    if prepopulate_registrations:
        ledger = root / "review_events.jsonl"
        ledger.write_text("".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events),
                          encoding="utf-8")
    return registrations, candidate_index


def _percentile95(samples: list[float]) -> float:
    ordered = sorted(samples)
    return ordered[max(0, math.ceil(.95 * len(ordered)) - 1)]


def measure(count: int, source_count: int, repeats: int) -> dict:
    if source_count > count:
        raise ValueError("source count cannot exceed candidate count")
    temp_root = Path(__file__).resolve().parents[1] / ".test-tmp"
    temp_root.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="recipe-catalog-benchmark-", dir=temp_root) as directory:
        root = Path(directory)
        cold_root = root / "cold"
        cold_root.mkdir()
        _prepopulate(cold_root, count, source_count, prepopulate_registrations=False)
        assert not (cold_root / "review_events.jsonl").exists(), "cold fixture already has registrations"
        cold_started = time.perf_counter()
        cold = load_catalog(cold_root)
        cold_registration_seconds = time.perf_counter() - cold_started
        cold_ledger = cold_root / "review_events.jsonl"
        cold_events = [line for line in cold_ledger.read_text(encoding="utf-8").splitlines() if line.strip()]
        assert len(cold) == count
        assert len(cold_events) == count, "cold load did not durably register every candidate"

        registrations, prepared = _prepopulate(root, count, source_count)
        ledger_path = root / "review_events.jsonl"
        ledger_size = ledger_path.stat().st_size
        warmup_started = time.perf_counter()
        initial = load_catalog(root)
        warmup_seconds = time.perf_counter() - warmup_started
        assert len(initial) == count
        assert ledger_path.stat().st_size == ledger_size, "warm load unexpectedly appended registrations"

        samples = []
        for _ in range(repeats):
            started = time.perf_counter()
            assert len(load_catalog(root)) == count
            samples.append(time.perf_counter() - started)

        candidate_id, source_hash, extraction_id, candidate = registrations[0]
        source_path = root / "sources" / f"source-0000.jpg"
        reviews = ReviewStore(ledger_path)
        reviews.save_revision(
            candidate_id=candidate_id, source_hash=source_hash, extraction_id=extraction_id,
            expected_parent=0, operation_id="benchmark-external-revision",
            recipe=candidate.model_dump(mode="json"), target_bindings=[],
        )
        reviews.approve_revision(
            candidate_id=candidate_id, source_hash=source_hash,
            extraction_id=extraction_id, revision=1,
            operation_id="benchmark-external-approval", source_path=source_path,
        )
        approval_reload_started = time.perf_counter()
        refreshed = load_catalog(root)
        approval_reload_seconds = time.perf_counter() - approval_reload_started
        approved_view = next(view for view in refreshed if view.candidate_id == candidate_id)
        assert approved_view.revision == 1 and approved_view.import_ready

        return {
            "sources": source_count, "candidates": count, "prepopulated_registrations": prepared,
            "cold_load_and_registration_seconds": cold_registration_seconds,
            "cold_registration_events": len(cold_events),
            "warmup_seconds": warmup_seconds,
            "warm_p50_seconds": statistics.median(samples),
            "warm_p95_seconds": _percentile95(samples),
            "warm_samples_seconds": samples,
            "external_approval_reload_seconds": approval_reload_seconds,
            "external_approval_visible": True,
        }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidates", type=int, default=1000)
    parser.add_argument("--sources", type=int, default=100)
    parser.add_argument("--repeats", type=int, default=5)
    args = parser.parse_args()
    if args.candidates < 1 or args.sources < 1 or args.repeats < 1:
        parser.error("candidates, sources and repeats must be positive")
    print(json.dumps({"python": platform.python_version(), "platform": platform.platform(),
                      "method": "cold multi-source load and durable registration, then warm catalog loads with prepopulated registrations; approval appended externally",
                      "measurement": measure(args.candidates, args.sources, args.repeats)}, indent=2))
