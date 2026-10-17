import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from tests.ui_fixture_factory import write_ui_fixture


APP_PATH = Path(__file__).resolve().parents[1] / "streamlit_app.py"


class StreamlitNavigationTests(unittest.TestCase):
    def test_entrypoint_uses_native_top_navigation(self):
        source = APP_PATH.read_text(encoding="utf-8")

        self.assertIn('position="top"', source)
        self.assertNotIn("page_icon=\"🍲\"", source)

    def test_review_queue_exposes_filters_progress_and_safe_navigation(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = write_ui_fixture(Path(tmp))
            with patch.dict(os.environ, {
                "RECIPE_IMPORTER_OUTPUT_DIR": str(fixture["output"]),
                "RECIPE_IMPORTER_SOURCE_DIR": str(Path(tmp, "sources")),
            }, clear=False):
                app = AppTest.from_file(str(APP_PATH)).run(timeout=10)

        self.assertFalse(app.exception)
        self.assertTrue(any(item.value == "Review" for item in app.title))
        self.assertTrue(any("Ready to review · 1 of" in item.value for item in app.caption))
        self.assertTrue(any(button.label == "Back" for button in app.button))
        self.assertTrue(any(button.label.startswith("Skipped recipes (") for button in app.button))
        self.assertTrue(any(button.label == "Skip for now" for button in app.button))
        self.assertTrue(any(button.label == "Approve & next" for button in app.button))
        self.assertTrue(any(button.label == "Previous page" for button in app.button))
        self.assertTrue(any(button.label == "Next page" for button in app.button))
        self.assertIn("Search title or source", [item.label for item in app.text_input])
        self.assertTrue({"Show", "Source file", "Status", "Jump to recipe"}.issubset(
            {item.label for item in app.selectbox}))
        self.assertTrue({"Page", "Source view"}.issubset({item.label for item in app.selectbox}))
        self.assertIn("Workspace view", [item.label for item in app.radio])


if __name__ == "__main__":
    unittest.main()
