"""Realistic, sanitized data for the P0 review performance investigation."""
from __future__ import annotations

import json
import random
from pathlib import Path

import fitz
from PIL import Image

from import_recipes import file_digest
from recipe_quality import ExtractionBatch, RecipeCandidate
from draft_store import DraftStore
from review_store import ReviewStore, extraction_identity


def write_p0_fixture(root: Path, *, recipe_count: int = 500,
                     history_count: int = 700) -> dict:
    """Write a marked synthetic library; caller must use it only in isolation."""
    if not 350 <= recipe_count <= 1000:
        raise ValueError("recipe_count must be between 350 and 1,000")
    if history_count < 300:
        raise ValueError("history_count must include at least 300 history rows")
    output, sources = root / "output", root / "sources"
    extracted = output / "extracted"
    for folder in (extracted, sources / "cookbook-a", sources / "cookbook-b"):
        folder.mkdir(parents=True, exist_ok=True)
    (output / ".fixture-library").write_text("synthetic P0 library", encoding="utf-8")
    for prior_cache in extracted.glob("*.json"):
        prior_cache.unlink()
    (output / "review_events.jsonl").write_text("", encoding="utf-8")

    # A deterministic raster scan keeps this synthetic fixture large but stable.
    raster = sources / "cookbook-b" / "synthetic-large-scan.jpg"
    randomizer = random.Random(9137)
    pixels = bytes(randomizer.randrange(256) for _ in range(2200 * 1800 * 3))
    Image.frombytes("RGB", (2200, 1800), pixels).save(raster, quality=88)

    # Several scanned pages make source preview/rendering representative.
    scan = sources / "cookbook-a" / "synthetic-large-scan.pdf"
    document = fitz.open()
    for page_number in range(3):
        page = document.new_page(width=1800, height=2400)
        page.insert_image(page.rect, filename=str(raster))
        page.insert_text((96, 130), f"Synthetic recipe page {page_number + 1}", fontsize=28)
        page.insert_textbox((96, 180, 1600, 2200), "Synthetic source text. " * 2400, fontsize=16)
    document.save(scan)
    document.close()
    digest = file_digest(scan)

    recipes = []
    for index in range(recipe_count):
        title = "Duplicate Test Recipe" if index in (7, 211) else f"Synthetic Recipe {index:04d}"
        recipes.append(RecipeCandidate(
            name=title, ingredients=[f"{index + 1} cups synthetic ingredient", "salt"],
            instructions=["Stir the synthetic ingredients.", "Bake until done."],
        ))
    batch = ExtractionBatch(recipes=recipes)
    cache = {"schema_version": 2, "source_hash": digest,
             "batch": batch.model_dump(mode="json")}
    (extracted / f"{digest}.json").write_text(json.dumps(cache), encoding="utf-8")

    # Establish the stable registrations before adding import history. This
    # models the real lifecycle and leaves only deliberate duplicate titles
    # unbound when the catalog is loaded again.
    reviews = ReviewStore(output / "review_events.jsonl")
    registered = reviews.register_candidates(
        str(scan), digest, extraction_identity(digest, batch.model_dump(mode="json")), recipes,
    )
    extraction_id = extraction_identity(digest, batch.model_dump(mode="json"))
    # Include saved revision chains, one exact approval, and recoverable drafts
    # so review-ledger projection and navigation see realistic mixed history.
    for index in range(300, 310):
        registration = registered[index]
        recipe = recipes[index].model_dump(mode="json")
        first = reviews.save_revision(
            candidate_id=registration["candidate_id"], source_hash=digest,
            extraction_id=extraction_id, expected_parent=0,
            operation_id=f"p0-fixture-{index}-revision-1", recipe=recipe,
            target_bindings=registration["target_bindings"],
        )
        edited = dict(recipe, description=f"Synthetic reviewed revision {index}.")
        second = reviews.save_revision(
            candidate_id=registration["candidate_id"], source_hash=digest,
            extraction_id=extraction_id, expected_parent=int(first["revision"]),
            operation_id=f"p0-fixture-{index}-revision-2", recipe=edited,
            target_bindings=registration["target_bindings"],
        )
        if index == 300:
            reviews.approve_revision(
                candidate_id=registration["candidate_id"], source_hash=digest,
                extraction_id=extraction_id, revision=int(second["revision"]),
                operation_id=f"p0-fixture-{index}-approval", source_path=scan,
            )
    drafts = DraftStore(output / "ui_drafts.jsonl")
    (output / "ui_drafts.jsonl").write_text("", encoding="utf-8")
    for index in range(310, 360):
        drafts.save(registered[index]["candidate_id"], digest, str(scan),
                    recipes[index].model_dump(mode="json"), "", "", "structured", [])

    # Historical records include modern identities, legacy keys, mixed source
    # folders, imported/unresolved states, and paths that are intentionally gone.
    records = []
    for index in range(history_count):
        recipe_index = index % recipe_count
        candidate = recipes[recipe_index]
        is_import = index < min(125, recipe_count)
        historical_hash = digest if is_import else f"{index:064x}"
        key = f"{historical_hash}:{recipe_index}"
        identity = registered[recipe_index]["candidate_id"] if is_import else ""
        folder = "cookbook-a" if index % 2 else "cookbook-b"
        source_path = str((sources / folder / f"historical-{index}.jpg") if index % 5 else root / "missing" / f"{index}.pdf")
        status = "IMPORTED" if is_import else "INCOMPLETE"
        record = {"key": key, "status": status, "slug": f"synthetic-{index}",
                  "file": source_path, "title": candidate.name,
                  "timestamp": "2026-10-17T00:00:00+00:00"}
        if identity:
            record["identity"] = identity
        records.append(json.dumps(record))
    (output / "import_state.jsonl").write_text("\n".join(records) + "\n", encoding="utf-8")
    return {"output": output, "source_root": sources, "source": scan,
            "source_hash": digest, "recipe_count": recipe_count,
            "history_count": history_count, "expected": recipes,
            "registered_ids": [item["candidate_id"] for item in registered]}


def write_p0_source_groups(root: Path, *, source_count: int = 100,
                           recipes_per_source: int = 5) -> dict:
    """Build mixed-folder catalog data for repeated source-path lookup checks."""
    if source_count < 50 or not 1 <= recipes_per_source <= 20:
        raise ValueError("source_count must be at least 50; recipes_per_source must be 1..20")
    output, sources = root / "output", root / "sources"
    extracted = output / "extracted"
    extracted.mkdir(parents=True, exist_ok=True)
    (output / ".fixture-library").write_text("synthetic P0 source groups", encoding="utf-8")
    for prior_cache in extracted.glob("*.json"):
        prior_cache.unlink()
    (output / "review_events.jsonl").write_text("", encoding="utf-8")
    groups = []
    registration_batches = []
    for group in range(source_count):
        folder = sources / f"cookbook-{group % 8:02d}"
        folder.mkdir(parents=True, exist_ok=True)
        suffix = ".pdf" if group == 0 else ".jpg"
        source = folder / f"scan-{group:03d}{suffix}"
        if group == 0:
            doc = fitz.open()
            page = doc.new_page(width=800, height=1000)
            page.insert_text((50, 50), "Large synthetic source used for preview checks.")
            doc.save(source)
            doc.close()
        else:
            pixels = bytes([group % 256, (group * 3) % 256, (group * 7) % 256]) * (180 * 240)
            Image.frombytes("RGB", (180, 240), pixels).save(source, quality=75)
        digest = file_digest(source)
        recipes = [RecipeCandidate(
            name=f"Source {group:03d} Recipe {index:02d}",
            ingredients=[f"{index + 1} cups synthetic food"],
            instructions=["Cook the synthetic food."],
        ) for index in range(recipes_per_source)]
        batch = ExtractionBatch(recipes=recipes)
        extraction_id = extraction_identity(digest, batch.model_dump(mode="json"))
        (extracted / f"{digest}.json").write_text(json.dumps({
            "schema_version": 2, "source_hash": digest,
            "batch": batch.model_dump(mode="json"),
        }), encoding="utf-8")
        source_hint = str(sources / "missing" / source.name) if group % 10 == 9 else str(source)
        registration_batches.append((source_hint, digest, extraction_id, recipes))
        groups.append({"source": source, "source_hash": digest,
                       "recipes": recipes, "source_hint": source_hint,
                       "missing": group % 10 == 9})
        if group % 10 == 9:
            source.unlink()
    reviews = ReviewStore(output / "review_events.jsonl")
    registrations = reviews.register_candidate_batches(registration_batches)
    return {"output": output, "source_root": sources, "groups": groups,
            "registrations": registrations,
            "recipe_count": source_count * recipes_per_source,
            "missing_sources": sum(item["missing"] for item in groups)}
