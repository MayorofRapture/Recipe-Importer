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
import pymupdf as fitz

from batch_store import BatchStore
from import_recipes import file_digest
from recipe_quality import Diagnostic, ExtractionBatch, RecipeCandidate, Suggestion, schema_recipe
from review_store import ReviewStore, _candidate_payload, _canonical, extraction_identity
from tests.ui_fixture_factory import write_ui_fixture
from ui_data import load_catalog


def prepare(root: Path, scenario: str, run_id: str, nonce: str) -> dict:
    diagnostics = None
    if scenario in {"review-d2", "review-d2-corrections"}:
        if scenario == "review-d2-corrections":
            diagnostics = [
                Diagnostic(field_path="ingredients[0]", kind="ambiguous", severity="critical",
                           detail="First row issue.", source_evidence="The first row is blurred.",
                           suggested_action="Check the first synthetic ingredient."),
                Diagnostic(field_path="ingredients[1]", kind="unreadable", severity="critical",
                           detail="Second row issue.", source_evidence="The second row is smudged.",
                           suggested_action="Check the second synthetic ingredient."),
            ]
        else:
            diagnostics = [
                Diagnostic(field_path="name", kind="missing_from_source", severity="critical",
                           detail="The title is absent from the source.",
                           suggested_action="Confirm a title using another source."),
                Diagnostic(field_path="ingredients[0]", kind="ambiguous", severity="critical",
                           detail="The amount is unclear.", source_evidence="The left edge is cropped.",
                           suggested_action="Check the source before approving."),
                Diagnostic(field_path="ingredients[1]", kind="unreadable", severity="critical",
                           detail="The amount cannot be read.", source_evidence="The ink is smudged.",
                           suggested_action="Compare the scan before approving."),
                Diagnostic(field_path="instructions[0]", kind="not_extracted", severity="critical",
                           detail="A visible direction was not extracted.",
                           source_evidence="A direction appears below the ingredient list.",
                           suggested_action="Correct the step from the source."),
                Diagnostic(field_path="yield_text", kind="missing_from_source", severity="optional",
                           detail="The source does not state a yield.",
                           suggested_action="Leave blank if unknown."),
            ]
    fixture = write_ui_fixture(
        root, cranberry_diagnostics=diagnostics,
        cranberry_alternative_source_supported=scenario in {"review-d2", "review-d2-corrections"},
    )
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

    cookbook_batch_id = ""
    cookbook_filename = ""
    if scenario in {"upload", "cookbook"}:
        # A first-run Upload journey needs an empty synthetic library so app startup does not redirect to Review.
        for cached_batch in (output / "extracted").glob("*.json"):
            cached_batch.unlink()
        for ledger_name in ("review_events.jsonl", "import_state.jsonl"):
            (output / ledger_name).unlink(missing_ok=True)

    if scenario == "cookbook":
        cookbook_source = root / "synthetic-whole-cookbook.pdf"
        document = fitz.open()
        for page_number in range(1, 4):
            page = document.new_page(width=600, height=800)
            page.insert_text((72, 72), f"Synthetic whole cookbook page {page_number}")
            page.insert_text((72, 115), "Synthetic lentil soup\\n2 cups synthetic lentils\\nSimmer until tender.")
        document.save(cookbook_source)
        document.close()
        batch_store = BatchStore(output, parent_disk_reserve_bytes=0)
        cookbook_batch = batch_store.create("Synthetic cookbook batch")
        staged_cookbook = batch_store.stage_parent_pdf(cookbook_batch["batch_id"], cookbook_source)
        cookbook_batch_id = cookbook_batch["batch_id"]
        cookbook_filename = staged_cookbook["original_filename"]

    if scenario in {"p0", "review-state"}:
        manual_source = sources / ("synthetic-manual-instructions.pdf" if scenario == "review-state"
                                   else "synthetic-manual-instructions.png")
        if scenario == "review-state":
            manual_document = fitz.open()
            page = manual_document.new_page(width=600, height=800)
            page.insert_text((60, 70), "Synthetic Manual Directions")
            page.insert_text((60, 105), "1 cup synthetic beans")
            directions_page = manual_document.new_page(width=600, height=800)
            directions_page.insert_text((60, 70), "1. Stir the synthetic beans.\n2. Serve warm.")
            manual_document.save(manual_source)
            manual_document.close()
        else:
            manual_image = Image.new("RGB", (600, 800), "white")
            ImageDraw.Draw(manual_image).text(
                (60, 70), "Synthetic Manual Directions\n\n1 cup synthetic beans\n\nNo directions on source.",
                fill="black", spacing=18)
            manual_image.save(manual_source)
        manual_recipe = RecipeCandidate(
            name="Synthetic Manual Directions", ingredients=["1 cup synthetic beans"],
            instructions=( ["Stir the synthetic beans.", "Serve warm."]
                           if scenario == "review-state" else []), requires_review=True,
        )
        manual_batch = ExtractionBatch(recipes=[manual_recipe])
        manual_hash = file_digest(manual_source)
        extraction_id = extraction_identity(manual_hash, manual_batch.model_dump(mode="json"))
        cache_path = output / "extracted" / f"{manual_hash}.json"
        cache_path.write_text(json.dumps({"schema_version": 2, "source_hash": manual_hash,
                                         "batch": manual_batch.model_dump(mode="json")}),
                              encoding="utf-8")
        registration = ReviewStore(output / "review_events.jsonl").register_candidates(
            str(manual_source), manual_hash, extraction_id, [manual_recipe])
        if scenario == "p0":
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
        else:
            manual_candidate_id = registration[0]["candidate_id"]

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
    if scenario == "history":
        view = next(item for item in load_catalog(output, sources)
                    if item.candidate.name == "Fried Rice")
        attempt_id = "synthetic-historical-cli-attempt"
        intent = {
            "event": "intent_started", "schema_version": 1, "attempt_id": attempt_id,
            "plan_id": "cli", "candidate_id": view.candidate_id,
            "source_hash": view.source_hash, "extraction_id": view.extraction_id,
            "revision": view.revision, "title": view.candidate.name,
            "source_path": str(fixture["source"]), "batch_tag": "",
        }
        (output / "ui_import_events.jsonl").write_text("\n".join((
            json.dumps(intent), json.dumps({
                "event": "create_uncertain", "attempt_id": attempt_id,
                "candidate_id": view.candidate_id, "category": "timeout",
            }),
        )) + "\n", encoding="utf-8")
        (output / "fixture_mealie.json").write_text(json.dumps({
            "recipes": {"historical-existing": {
                "id": "synthetic-mealie-recipe-id", "slug": "historical-existing",
                "name": view.candidate.name, "tags": [],
                "payload": schema_recipe(view.candidate),
            }}, "tags": {},
        }), encoding="utf-8")
        marker_candidates = ["Fried Rice"]
    if scenario in {"p0", "review-state"}:
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
        "cookbook_batch_id": cookbook_batch_id, "cookbook_filename": cookbook_filename,
    }


if __name__ == "__main__":
    print(json.dumps(prepare(Path(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4])))
