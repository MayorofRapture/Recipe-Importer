import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from tests.ui_fixture_factory import write_ui_fixture

APP_PATH = Path(__file__).resolve().parents[1] / "streamlit_app.py"


class StreamlitReviewTests(unittest.TestCase):
    def test_review_page_loads_sanitized_catalog_without_external_services(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = write_ui_fixture(Path(tmp))
            with patch.dict(os.environ, {
                "RECIPE_IMPORTER_OUTPUT_DIR": str(fixture["output"]),
                "RECIPE_IMPORTER_SOURCE_DIR": str(Path(tmp, "sources")),
            }, clear=False):
                app = AppTest.from_file(str(APP_PATH)).run(timeout=10)
                app.segmented_control[0].set_value("All records").run(timeout=10)

        self.assertFalse(app.exception)
        self.assertEqual(app.title[0].value, "Review recipes")
        recipe_selector = next(item for item in app.selectbox if item.label == "Recipe")
        self.assertIn("Fried Rice", recipe_selector.options)
        self.assertIn("Imported Recipe", recipe_selector.options)

    def test_review_page_exposes_safe_draft_action(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = write_ui_fixture(Path(tmp))
            with patch.dict(os.environ, {
                "RECIPE_IMPORTER_OUTPUT_DIR": str(fixture["output"]),
                "RECIPE_IMPORTER_SOURCE_DIR": str(Path(tmp, "sources")),
            }, clear=False):
                app = AppTest.from_file(str(APP_PATH)).run(timeout=10)

        self.assertFalse(app.exception)
        self.assertTrue(any("Save draft" in button.label for button in app.button))

    def test_save_draft_writes_separate_ui_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = write_ui_fixture(Path(tmp))
            with patch.dict(os.environ, {
                "RECIPE_IMPORTER_OUTPUT_DIR": str(fixture["output"]),
                "RECIPE_IMPORTER_SOURCE_DIR": str(Path(tmp, "sources")),
            }, clear=False):
                app = AppTest.from_file(str(APP_PATH)).run(timeout=10)
                next(button for button in app.button if button.label == "Save draft").click().run(timeout=10)

            draft_path = Path(fixture["output"]) / "ui_drafts.jsonl"
            self.assertTrue(draft_path.exists())
            self.assertIn("Fried Rice", draft_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
