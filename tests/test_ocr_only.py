import csv
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from import_recipes import _run_ocr_only, main
from local_ocr import OcrResult


class OcrOnlyTests(unittest.TestCase):
    def test_ocr_only_retains_original_layout_and_selected_variants(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "recipes"
            source.mkdir()
            (source / "caramel.jpg").write_bytes(b"fixture")
            work = root / "output"
            original = "The Best Caramel Apples\nStep 1\nStep 2"
            layout = "Step 2\nStep 1\nThe Best Caramel Apples"
            result = OcrResult.from_variants(original, layout)

            with patch("import_recipes.local_ocr.extract_local", return_value=result):
                _run_ocr_only(source, work, limit=1, workers=1, tesseract_cmd="")

            with (work / "last_run_report.csv").open(encoding="utf-8-sig", newline="") as report_file:
                row = next(csv.DictReader(report_file))

            original_path = Path(row["ocr_original_text_path"])
            layout_path = Path(row["ocr_layout_text_path"])
            selected_path = Path(row["ocr_selected_text_path"])
            original_saved = original_path.read_text(encoding="utf-8")
            layout_saved = layout_path.read_text(encoding="utf-8")
            selected_saved = selected_path.read_text(encoding="utf-8")

        self.assertEqual(original_saved, original)
        self.assertEqual(layout_saved, layout)
        self.assertEqual(selected_saved, result.text)
        self.assertEqual(row["ocr_selected_variant"], result.selected_variant)
        self.assertIn("original", row["ocr_selection_reason"])

    def test_ocr_only_is_idempotent_and_does_not_initialize_gemini_or_mealie(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "recipes"
            source.mkdir()
            (source / "scan.jpg").write_bytes(b"fixture")
            work = root / "output"
            result = OcrResult.from_text(
                "RECIPE 1: Jalapeno Peppers\nIngredients:\n2 peppers\n"
                "Instructions:\nRoast peppers"
            )

            with patch("import_recipes.local_ocr.extract_local", return_value=result) as ocr, \
                 patch("import_recipes.google_client") as google_client, \
                 patch("import_recipes.extract") as gemini_extract, \
                 patch("import_recipes.UsageLedger") as usage_ledger, \
                 patch("import_recipes.ModelRouter") as router, \
                 patch("import_recipes.MealieClient") as mealie_client, \
                 patch("import_recipes.preflight_mealie") as preflight:
                first_code = _run_ocr_only(source, work, limit=1, workers=1, tesseract_cmd="")
                second_code = _run_ocr_only(source, work, limit=1, workers=1, tesseract_cmd="")

            with (work / "last_run_report.csv").open(encoding="utf-8-sig", newline="") as report_file:
                report_rows = list(csv.DictReader(report_file))
            review_events = [json.loads(line) for line in
                             (work / "review_events.jsonl").read_text(encoding="utf-8").splitlines()]
            text_path_exists = Path(report_rows[0]["ocr_text_path"]).exists()

        self.assertEqual(first_code, 1)
        self.assertEqual(second_code, 1)
        self.assertEqual(ocr.call_count, 1)
        self.assertEqual([row["status"] for row in report_rows], ["OCR_NEEDS_REVIEW"])
        self.assertEqual(report_rows[0]["manual_review_required"], "True")
        self.assertTrue(text_path_exists)
        self.assertEqual(len(review_events), 1)
        google_client.assert_not_called()
        gemini_extract.assert_not_called()
        usage_ledger.assert_not_called()
        router.assert_not_called()
        mealie_client.assert_not_called()
        preflight.assert_not_called()

    def test_help_lists_ocr_only(self):
        output = io.StringIO()
        with patch("import_recipes.sys.argv", ["import_recipes.py", "--help"]), \
             patch("import_recipes.load_dotenv"), redirect_stdout(output):
            with self.assertRaises(SystemExit) as raised:
                main()

        self.assertEqual(raised.exception.code, 0)
        self.assertIn("--ocr-only", output.getvalue())


if __name__ == "__main__":
    unittest.main()
