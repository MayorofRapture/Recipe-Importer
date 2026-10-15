import unittest
import csv
import io
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from contextlib import redirect_stdout
from unittest.mock import patch

from import_recipes import (
    GeminiExtractionError,
    OCR_STRUCTURE_CACHE_VERSION,
    OCR_STRUCTURE_PROMPT_VERSION,
    StateStore,
    _run_ocr_structure,
    _load_ocr_structure_cache,
    _save_ocr_structure_cache,
    candidate_identity,
    file_digest,
    main,
    process_file,
    structure_ocr_text,
)
from local_ocr import OcrResult
from ocr_structuring import (
    LineSpan,
    OcrLineReferences,
    RecipeLineReferences,
    ReferenceValidationError,
    assemble_line_references,
)
from review_store import ReviewStore


class OcrStructureTests(unittest.TestCase):
    def test_layout_cache_version_ignores_legacy_structure_without_deleting_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            legacy = cache_dir / "digest--ocr-structure--python-primary--1.json"
            batch = OcrResult.from_text(
                "Fried Rice\nINGREDIENTS:\nrice\nINSTRUCTIONS:\nCook"
            ).batch
            legacy.write_text(
                json.dumps({
                    "schema_version": 3, "source_hash": "digest",
                    "prompt_version": "ocr-structure-v1",
                    "batch": batch.model_dump(mode="json"),
                }), encoding="utf-8")
            current = _save_ocr_structure_cache(cache_dir, "digest", "python", "local", batch)
            loaded = _load_ocr_structure_cache(cache_dir, "digest")
            legacy_preserved = legacy.exists()
            raw = json.loads(current.read_text(encoding="utf-8"))

        self.assertEqual(loaded[0], current)
        self.assertTrue(legacy_preserved)
        self.assertEqual(raw["schema_version"], OCR_STRUCTURE_CACHE_VERSION)
        self.assertEqual(raw["prompt_version"], OCR_STRUCTURE_PROMPT_VERSION)

    def test_help_lists_ocr_structure(self):
        output = io.StringIO()
        with patch("import_recipes.sys.argv", ["import_recipes.py", "--help"]), \
             patch("import_recipes.load_dotenv"), redirect_stdout(output):
            with self.assertRaises(SystemExit) as raised:
                main()

        self.assertEqual(raised.exception.code, 0)
        self.assertIn("--ocr-structure", output.getvalue())

    def test_deterministic_structure_reuses_ocr_and_writes_reviewable_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "fried-rice.jpg"
            source.write_bytes(b"fixture")
            output = root / "output"
            ocr_result = OcrResult.from_text(
                "1/18/2014 8:52:00 PM\n\nFried Rice\n\nINGREDIENTS:\n"
                "2 cups rice\nINSTRUCTIONS:\nCook rice."
            )
            router = SimpleNamespace(run=lambda *args, **kwargs:
                                     (_ for _ in ()).throw(AssertionError("Gemini should not run")))
            with patch("import_recipes.local_ocr.extract_local", return_value=ocr_result) as extract_local:
                first = _run_ocr_structure(
                    root, output, {"api_key": "", "model": "primary",
                                   "tesseract_cmd": ""}, router, limit=1, workers=1,
                )
                second = _run_ocr_structure(
                    root, output, {"api_key": "", "model": "primary",
                                   "tesseract_cmd": ""}, router, limit=1, workers=1,
                )

            with (output / "last_run_report.csv").open(encoding="utf-8-sig", newline="") as report_file:
                report_rows = list(csv.DictReader(report_file))
            cache_files = list((output / "extracted").glob("*ocr-structure*.json"))
            selected_variant = report_rows[0]["ocr_selected_variant"]
            original_path_exists = Path(report_rows[0]["ocr_original_text_path"]).exists()
            layout_path_exists = Path(report_rows[0]["ocr_layout_text_path"]).exists()
            cache_selected_variant = json.loads(
                cache_files[0].read_text(encoding="utf-8"))["ocr_selected_variant"]

        self.assertEqual(first, 1)
        self.assertEqual(second, 1)
        self.assertEqual(extract_local.call_count, 1)
        self.assertEqual(report_rows[0]["structuring_method"], "python")
        self.assertEqual(report_rows[0]["title"], "Fried Rice")
        self.assertEqual(report_rows[0]["status"], "OCR_STRUCTURED_NEEDS_REVIEW")
        self.assertEqual(len(cache_files), 1)
        self.assertEqual(selected_variant, "original")
        self.assertTrue(original_path_exists)
        self.assertTrue(layout_path_exists)
        self.assertEqual(cache_selected_variant, "original")

    def test_unstructured_ocr_uses_gemini_line_references_and_keeps_raw_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "unstructured.jpg"
            source.write_bytes(b"fixture")
            output = root / "output"
            text = "Mystery Dish\nrice\nCook rice"
            ocr_result = OcrResult.from_text(text)
            refs = OcrLineReferences(recipes=[RecipeLineReferences(
                title=LineSpan(start=1, end=1),
                ingredients=[LineSpan(start=2, end=2)],
                instructions=[LineSpan(start=3, end=3)],
                confidence="high",
            )])

            class Router:
                def run(self, attempt, quality_check=None):
                    value = attempt(SimpleNamespace(id="text-model"))
                    return SimpleNamespace(value=value, model_id="text-model",
                                           reason="primary", attempts=1)

            with patch("import_recipes.local_ocr.extract_local", return_value=ocr_result), \
                 patch("import_recipes.structure_ocr_text") as structure:
                structure.side_effect = lambda text, api_key, model, gate=None: refs
                rows = _run_ocr_structure(
                    root, output, {"api_key": "api-key", "model": "primary",
                                   "tesseract_cmd": ""}, Router(), limit=1, workers=1,
                )

            saved_text = (output / "ocr_text" / next(
                item.name for item in (output / "ocr_text").iterdir())).read_text(encoding="utf-8")
            with (output / "last_run_report.csv").open(encoding="utf-8-sig", newline="") as report_file:
                report_row = next(csv.DictReader(report_file))

        self.assertEqual(rows, 1)
        self.assertEqual(saved_text, text)
        self.assertEqual(report_row["structuring_method"], "gemini_text")
        self.assertEqual(report_row["gemini_model"], "text-model")
        structure.assert_called_once()

    def test_failed_text_structuring_registers_ocr_for_manual_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "unstructured.jpg"
            source.write_bytes(b"fixture")
            output = root / "output"
            ocr_result = OcrResult.from_text("Mystery Dish\nrice\nCook rice")

            class Router:
                def run(self, attempt, quality_check=None):
                    return attempt(SimpleNamespace(id="text-model"))

            error = GeminiExtractionError(
                "Gemini text structuring returned no text (RECITATION)",
                model="text-model", diagnostics={"reason": "RECITATION"},
            )
            with patch("import_recipes.local_ocr.extract_local", return_value=ocr_result), \
                 patch("import_recipes.structure_ocr_text", side_effect=error):
                result = _run_ocr_structure(
                    root, output, {"api_key": "api-key", "model": "primary",
                                   "tesseract_cmd": ""}, Router(), limit=1, workers=1,
                )

            with (output / "needs_review.csv").open(encoding="utf-8-sig", newline="") as review_file:
                review_rows = list(csv.DictReader(review_file))

        self.assertEqual(result, 1)
        self.assertEqual(len(review_rows), 1)
        self.assertEqual(review_rows[0]["review_status"], "pending")
        self.assertEqual(review_rows[0]["diagnosis"], "ambiguous")

    def test_approved_hybrid_cache_uses_existing_import_flow_without_duplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "recipes"
            source.mkdir()
            path = source / "fried-rice.jpg"
            path.write_bytes(b"fixture")
            output = root / "output"
            result = OcrResult.from_text(
                "Fried Rice\nINGREDIENTS:\n2 cups rice\n"
                "INSTRUCTIONS:\nCook rice."
            )
            router = SimpleNamespace(run=lambda *args, **kwargs:
                                     (_ for _ in ()).throw(AssertionError("Gemini should not run")))
            with patch("import_recipes.local_ocr.extract_local", return_value=result):
                self.assertEqual(_run_ocr_structure(
                    source, output, {"api_key": "", "model": "primary",
                                     "tesseract_cmd": ""}, router, limit=1, workers=1,
                ), 1)

            digest = file_digest(path)
            reviews = ReviewStore(output / "review_events.jsonl")
            identity = next(iter(reviews.candidates))
            reviews.record_candidate_decision(str(path), identity, "approved")
            state = StateStore(output / "import_state.jsonl")
            imported = []
            config = {"api_key": "", "mealie_url": "http://mealie",
                      "mealie_token": "token", "model": "primary"}
            with patch("import_recipes.import_mealie",
                       side_effect=lambda recipe, *_: imported.append(recipe) or "fried-rice"):
                first = process_file(
                    path, digest, config, None, state, output / "extracted", False,
                    review_store=reviews,
                )
                second = process_file(
                    path, digest, config, None, state, output / "extracted", False,
                    review_store=reviews,
                )

        self.assertEqual([row["status"] for row in first], ["IMPORTED"])
        self.assertEqual([row["status"] for row in second], ["ALREADY_DONE"])
        self.assertEqual(len(imported), 1)
        self.assertEqual(imported[0].instructions, ["Cook rice."])

    def test_fried_rice_is_structured_without_gemini(self):
        text = (
            "1/18/2014 8:52:00 PM\n\nFried Rice\n\nINGREDIENTS:\n"
            "2 1/2 cups uncooked white rice\n3 large eggs\n\n"
            "INSTRUCTIONS:\nCook your rice however you usually prep it.\n\n"
            "Scramble the eggs and fold them in."
        )

        result = OcrResult.from_text(text)

        self.assertTrue(result.structured)
        self.assertEqual(result.structuring_method, "python")
        self.assertEqual(result.batch.recipes[0].name, "Fried Rice")
        self.assertEqual(result.batch.recipes[0].ingredients, [
            "2 1/2 cups uncooked white rice", "3 large eggs",
        ])
        self.assertEqual(result.batch.recipes[0].instructions, [
            "Cook your rice however you usually prep it.",
            "Scramble the eggs and fold them in.",
        ])
        self.assertEqual(result.text, text)

    def test_fried_rice_joins_wrapped_instruction_paragraphs_and_preserves_ranges(self):
        text = (
            "Fried Rice\n"
            "INGREDIENTS:\n"
            "2 cups cooked white rice\n"
            "1 tablespoon vegetable oil\n"
            "1 small onion, diced\n"
            "2 cloves garlic, minced\n"
            "2 large eggs\n"
            "1/2 cup frozen peas\n"
            "2 tablespoons soy sauce\n"
            "2 green onions, sliced\n"
            "INSTRUCTIONS:\n"
            "1. Heat the oil in a large skillet or wok over\n"
            "medium-high heat. Add the onion and cook\n"
            "until softened and lightly browned, stirring\n"
            "frequently.\n"
            "2. Add the garlic and rice. Stir until every grain is\n"
            "coated and the rice is hot, about 3 minutes,\n"
            "stirring frequently to prevent sticking and\n"
            "ensure even browning.\n"
            "3. Push the rice to one side. Pour the eggs into the\n"
            "empty space and scramble until just set, then stir\n"
            "them into the rice until evenly distributed.\n"
            "Keep the heat at medium.\n"
            "4. Add the peas and soy sauce. Continue cooking until\n"
            "everything is heated through and the rice is slightly\n"
            "crisp and the peas are tender.\n"
            "Stir once more before serving.\n"
            "5. Season with salt and pepper, garnish with green\n"
            "onions, and serve immediately in bite-\n"
            "size portions."
        )

        result = OcrResult.from_text(text)
        candidate = result.batch.recipes[0]

        self.assertEqual(len(candidate.instructions), 5)
        self.assertEqual(candidate.instructions[0], (
            "1. Heat the oil in a large skillet or wok over "
            "medium-high heat. Add the onion and cook "
            "until softened and lightly browned, stirring "
            "frequently."
        ))
        self.assertEqual(candidate.instructions[-1], (
            "5. Season with salt and pepper, garnish with green "
            "onions, and serve immediately in bite-size portions."
        ))
        self.assertTrue(candidate.requires_review)
        self.assertTrue(candidate.source_line_ranges)
        self.assertEqual(
            [item for item in candidate.source_line_ranges if item["field"] == "instructions"],
            [
                {"field": "instructions", "start": 12, "end": 15},
                {"field": "instructions", "start": 16, "end": 19},
                {"field": "instructions", "start": 20, "end": 23},
                {"field": "instructions", "start": 24, "end": 27},
                {"field": "instructions", "start": 28, "end": 30},
            ],
        )

    def test_line_references_assemble_multiple_noncontiguous_recipes(self):
        text = """Fried Rice
noise between fields
INGREDIENTS:
2 cups rice
3 eggs
INSTRUCTIONS:
1. Cook rice.
2. Fold in eggs.

Salsa
INGREDIENTS:
2 tomatoes
INSTRUCTIONS:
Chop tomatoes.
"""
        refs = OcrLineReferences(recipes=[
            RecipeLineReferences(
                title=LineSpan(start=1, end=1),
                ingredients=[LineSpan(start=4, end=5)],
                instructions=[LineSpan(start=7, end=8)],
                confidence="high",
            ),
            RecipeLineReferences(
                title=LineSpan(start=10, end=10),
                ingredients=[LineSpan(start=12, end=12)],
                instructions=[LineSpan(start=14, end=14)],
                confidence="medium",
            ),
        ])

        batch = assemble_line_references(text, refs)

        self.assertEqual([recipe.name for recipe in batch.recipes], ["Fried Rice", "Salsa"])
        self.assertEqual(batch.recipes[0].ingredients, ["2 cups rice", "3 eggs"])
        self.assertEqual(batch.recipes[0].instructions, ["1. Cook rice.", "2. Fold in eggs."])
        self.assertEqual(batch.recipes[1].ingredients, ["2 tomatoes"])
        self.assertEqual(batch.recipes[1].instructions, ["Chop tomatoes."])
        self.assertTrue(all(recipe.requires_review for recipe in batch.recipes))

    def test_invalid_and_overlapping_line_references_are_rejected(self):
        text = "Title\nIngredients\nRice\nInstructions\nCook"
        out_of_bounds = OcrLineReferences(recipes=[RecipeLineReferences(
            title=LineSpan(start=1, end=1),
            ingredients=[LineSpan(start=2, end=9)],
            instructions=[LineSpan(start=5, end=5)],
        )])
        overlapping = OcrLineReferences(recipes=[RecipeLineReferences(
            title=LineSpan(start=1, end=1),
            ingredients=[LineSpan(start=2, end=3)],
            instructions=[LineSpan(start=3, end=5)],
        )])

        with self.assertRaises(ReferenceValidationError):
            assemble_line_references(text, out_of_bounds)
        with self.assertRaises(ReferenceValidationError):
            assemble_line_references(text, overlapping)

    def test_missing_referenced_field_becomes_review_diagnostic(self):
        refs = OcrLineReferences(recipes=[RecipeLineReferences(
            title=LineSpan(start=1, end=1),
            ingredients=[LineSpan(start=2, end=2)],
            instructions=[],
            confidence="low",
            uncertainties=["No instruction section was identified."],
        )])

        batch = assemble_line_references("Soup\n2 cups stock", refs)
        candidate = batch.recipes[0]

        self.assertFalse(candidate.instructions)
        self.assertTrue(any(item.field_path == "instructions" for item in candidate.missing_information))
        self.assertEqual(candidate.uncertainties, ["No instruction section was identified."])
        self.assertTrue(candidate.requires_review)

    def test_text_structuring_sends_numbered_text_without_image(self):
        captured = {}

        class Models:
            def generate_content(self, **kwargs):
                captured.update(kwargs)
                return SimpleNamespace(
                    text=OcrLineReferences(recipes=[RecipeLineReferences(
                        title=LineSpan(start=1, end=1),
                        ingredients=[LineSpan(start=2, end=2)],
                        instructions=[LineSpan(start=3, end=3)],
                    )]).model_dump_json(),
                    candidates=[], prompt_feedback=None, usage_metadata=None,
                )

        with patch("import_recipes.google_client", return_value=SimpleNamespace(models=Models())):
            refs = structure_ocr_text("Title\nRice\nCook", "api-key", "text-model")

        self.assertEqual(refs.recipes[0].title.start, 1)
        self.assertEqual(captured["model"], "text-model")
        self.assertEqual(captured["contents"][1], "1: Title\n2: Rice\n3: Cook")

    def test_text_structuring_preserves_recitation_diagnostics(self):
        class Models:
            def generate_content(self, **kwargs):
                return SimpleNamespace(
                    text="",
                    candidates=[SimpleNamespace(finish_reason="RECITATION", safety_ratings=[])],
                    prompt_feedback=None, usage_metadata=None,
                )

        with patch("import_recipes.google_client", return_value=SimpleNamespace(models=Models())):
            with self.assertRaises(GeminiExtractionError) as caught:
                structure_ocr_text("Title", "api-key", "text-model")

        self.assertEqual(caught.exception.diagnostics["reason"], "RECITATION")


if __name__ == "__main__":
    unittest.main()
