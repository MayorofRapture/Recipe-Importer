"""Create sanitized, disposable data for the owned browser fixture runner."""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from PIL import Image, ImageDraw

from import_recipes import file_digest
from recipe_quality import ExtractionBatch, RecipeCandidate, Suggestion
from review_store import ReviewStore, _candidate_payload, _canonical, extraction_identity
from tests.ui_fixture_factory import write_ui_fixture
from ui_data import load_catalog


def prepare(root: Path, scenario: str, run_id: str, nonce: str) -> dict:
    fixture = write_ui_fixture(root)
    output, sources = Path(fixture["output"]), Path(fixture["source"]).parent
    manual_candidate_id = ""
    expected = {"kind": "synthetic-browser-fixture", "run_id": run_id, "nonce": nonce}
    (root / ".recipe-importer-browser-fixture.json").write_text(
        json.dumps(expected), encoding="utf-8")
    (output / ".fixture-library").write_text(json.dumps(expected), encoding="utf-8")
    (sources / ".fixture-sources").write_text(json.dumps(expected), encoding="utf-8")

    upload_root = root / "uploads"
    upload_root.mkdir()
    valid_file = upload_root / "synthetic-recipe.png"
    image = Image.new("RGB", (600, 800), "white")
    ImageDraw.Draw(image).text((60, 70), "Synthetic browser recipe\n\n2 cups synthetic rice\n\nCook the synthetic rice.",
                               fill="black", spacing=18)
    image.save(valid_file)
    invalid_file = upload_root / "invalid-scan.pdf"
    invalid_file.write_text("Synthetic invalid upload.", encoding="utf-8")

    if scenario == "upload":
        # A first-run Upload journey needs an empty synthetic library so app startup does not redirect to Review.
        for cached_batch in (output / "extracted").glob("*.json"):
            cached_batch.unlink()
        for ledger_name in ("review_events.jsonl", "import_state.jsonl"):
            (output / ledger_name).unlink(missing_ok=True)

    if scenario == "p0":
        manual_source = sources / "synthetic-manual-instructions.png"
        manual_image = Image.new("RGB", (600, 800), "white")
        ImageDraw.Draw(manual_image).text(
            (60, 70), "Synthetic Manual Directions\n\n1 cup synthetic beans\n\nNo directions on source.",
            fill="black", spacing=18)
        manual_image.save(manual_source)
        manual_recipe = RecipeCandidate(
            name="Synthetic Manual Directions", ingredients=["1 cup synthetic beans"],
            instructions=[], requires_review=True,
        )
        manual_batch = ExtractionBatch(recipes=[manual_recipe])
        manual_hash = file_digest(manual_source)
        extraction_id = extraction_identity(manual_hash, manual_batch.model_dump(mode="json"))
        cache_path = output / "extracted" / f"{manual_hash}.json"
        cache_path.write_text(json.dumps({"schema_version": 2, "source_hash": manual_hash,
                                         "batch": manual_batch.model_dump(mode="json")}),
                              encoding="utf-8")
        ReviewStore(output / "review_events.jsonl").register_candidates(
            str(manual_source), manual_hash, extraction_id, [manual_recipe])
        view = next(item for item in load_catalog(output)
                    if item.candidate.name == "Fried Rice")
        manual_candidate_id = view.candidate_id
        saved_recipe = view.candidate.model_copy(update={
            "name": "Browser saved manual title",
            "yield_text": "3 servings",
            "description": "Synthetic saved description.",
            "ingredients": ["4 cups synthetic rice", "2 synthetic eggs"],
            "instructions": ["Heat the synthetic rice.", "Fold in the synthetic eggs."],
            "notes": ["Synthetic saved note."],
        })
        ReviewStore(output / "review_events.jsonl").save_revision(
            candidate_id=view.candidate_id, source_hash=view.source_hash,
            extraction_id=view.extraction_id, expected_parent=0,
            operation_id="browser-p0-manual-saved-revision",
            recipe=saved_recipe.model_dump(mode="json"),
            target_bindings=list(view.target_bindings),
        )
        Path(fixture["source"]).unlink()

    if scenario == "identity":
        (output / "import_state.jsonl").unlink(missing_ok=True)
        base = next(view for view in load_catalog(output) if view.candidate.name == "Fried Rice")
        store = ReviewStore(output / "review_events.jsonl")
        for index in range(17):
            source_hash = f"synthetic-source-{index}"
            recipe = RecipeCandidate(name=f"Synthetic blend {index}",
                                     ingredients=[f"{index + 1} tsp salt"], instructions=["Stir."])
            prior = store.register_candidates("synthetic.jpg", source_hash,
                                              f"old-{index}", [recipe])[0]
            updated = recipe.model_copy(update={"suggestions": [Suggestion(
                suggestion_id=f"suggestion-{index}", field_path="instructions[0]",
                proposed_value="Stir thoroughly.", rationale="Synthetic proposal.",
            )]})
            payload = _candidate_payload(updated)
            held_id, extraction_id = f"held-{index}", f"new-{index}"
            event = store._base_event("candidate_registration", held_id, source_hash,
                                      extraction_id, f"held-registration-{index}")
            event.update({"source": "synthetic.jpg", "fingerprint": hashlib.sha256(
                _canonical(payload).encode()).hexdigest(), "identity_status": "needs_reconciliation",
                "candidate": payload, "legacy_aliases": [], "target_bindings": []})
            with (output / "review_events.jsonl").open("a", encoding="utf-8") as ledger:
                ledger.write(json.dumps(event) + "\n")
            store = ReviewStore(output / "review_events.jsonl")

        updated_visible = base.candidate.model_copy(update={"suggestions": [Suggestion(
            suggestion_id="visible-proposal", field_path="instructions[0]",
            proposed_value="Fold in gently.", rationale="Synthetic proposal.",
        )]})
        seeded_bindings = ReviewStore(output / ".identity-target-bindings.jsonl").register_candidates(
            str(fixture["source"]), base.source_hash,
            extraction_identity(base.source_hash, {
                "recipes": [updated_visible.model_dump(mode="json")], "general_warnings": [],
            }), [updated_visible],
        )[0]["target_bindings"]
        extraction_id = extraction_identity(base.source_hash, {
            "recipes": [updated_visible.model_dump(mode="json")], "general_warnings": [],
        })
        payload = _candidate_payload(updated_visible)
        event = store._base_event("candidate_registration", "held-visible", base.source_hash,
                                  extraction_id, "held-visible-registration")
        event.update({"source": str(fixture["source"]), "fingerprint": hashlib.sha256(
            _canonical(payload).encode()).hexdigest(), "identity_status": "needs_reconciliation",
            "candidate": payload, "legacy_aliases": [], "target_bindings": seeded_bindings})
        with (output / "review_events.jsonl").open("a", encoding="utf-8") as ledger:
            ledger.write(json.dumps(event) + "\n")
        (output / "extracted" / f"{base.source_hash}--batch-v2.json").write_text(
            json.dumps({"source_hash": base.source_hash, "batch": {
                "recipes": [updated_visible.model_dump(mode="json")], "general_warnings": [],
            }}), encoding="utf-8")

    marker_candidates = [
        "Fried Rice", "The Best Caramel Apples", "Cranberry & Caramel Date Bars",
        "Imported Recipe",
    ]
    if scenario == "identity":
        marker_candidates = ["Fried Rice"]
    if scenario == "p0":
        marker_candidates.append("Synthetic Manual Directions")
    candidate_ids: dict[str, list[str]] = {}
    for view in load_catalog(output, sources):
        candidate_ids.setdefault(view.candidate.name, []).append(view.candidate_id)
    return {
        "root": str(root.resolve()), "output": str(output.resolve()),
        "sources": str(sources.resolve()), "valid_file": str(valid_file.resolve()),
        "invalid_file": str(invalid_file.resolve()), "source_file": str(fixture["source"]),
        "candidates": marker_candidates, "candidate_ids": candidate_ids,
        "manual_candidate_id": manual_candidate_id,
    }


if __name__ == "__main__":
    print(json.dumps(prepare(Path(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4])))
