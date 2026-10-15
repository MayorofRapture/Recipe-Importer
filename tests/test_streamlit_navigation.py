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

    def test_review_queue_is_horizontal_and_reports_progress(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = write_ui_fixture(Path(tmp))
            with patch.dict(os.environ, {
                "RECIPE_IMPORTER_OUTPUT_DIR": str(fixture["output"]),
                "RECIPE_IMPORTER_SOURCE_DIR": str(Path(tmp, "sources")),
            }, clear=False):
                app = AppTest.from_file(str(APP_PATH)).run(timeout=10)

        self.assertFalse(app.exception)
        self.assertTrue(any("Recipe 1 of" in item.value for item in app.caption))
        self.assertTrue(any("Previous recipe" in button.label for button in app.button))
        self.assertTrue(any("Next recipe" in button.label for button in app.button))
        self.assertIn("Find a recipe", [item.label for item in app.text_input])
        self.assertIn("Batch", [item.label for item in app.selectbox])


if __name__ == "__main__":
    unittest.main()
