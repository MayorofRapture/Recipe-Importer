import json
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
                next(item for item in app.selectbox if item.label == "Show").set_value("All recipes").run(timeout=10)

        self.assertFalse(app.exception)
        self.assertTrue(any(item.label == "Recipe title" for item in app.text_input))
        recipe_selector = next(item for item in app.selectbox if item.label == "Jump to recipe")
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

    def test_save_draft_writes_authoritative_revision_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = write_ui_fixture(Path(tmp))
            with patch.dict(os.environ, {
                "RECIPE_IMPORTER_OUTPUT_DIR": str(fixture["output"]),
                "RECIPE_IMPORTER_SOURCE_DIR": str(Path(tmp, "sources")),
            }, clear=False):
                app = AppTest.from_file(str(APP_PATH)).run(timeout=10)
                next(button for button in app.button if button.label == "Save draft").click().run(timeout=10)

            review_path = Path(fixture["output"]) / "review_events.jsonl"
            events = [json.loads(line) for line in review_path.read_text(encoding="utf-8").splitlines()]
            self.assertTrue(any(event.get("event") == "review_revision"
                                and event.get("recipe", {}).get("name") == "Fried Rice"
                                for event in events))
            self.assertFalse((Path(fixture["output"]) / "ui_drafts.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
