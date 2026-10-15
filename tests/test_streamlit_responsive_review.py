import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from tests.ui_fixture_factory import write_ui_fixture


APP_PATH = Path(__file__).resolve().parents[1] / "streamlit_app.py"
REVIEW_PATH = APP_PATH.parent / "app_pages" / "review.py"


class StreamlitResponsiveReviewTests(unittest.TestCase):
    def test_review_uses_bounded_scroll_regions_and_source_alt_text(self):
        source = REVIEW_PATH.read_text(encoding="utf-8")

        self.assertIn("height=650", source)
        self.assertIn("alt=", source)

    def test_approve_action_is_enabled_for_ready_recipe_and_blocked_for_critical_recipe(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = write_ui_fixture(Path(tmp))
            with patch.dict(os.environ, {
                "RECIPE_IMPORTER_OUTPUT_DIR": str(fixture["output"]),
                "RECIPE_IMPORTER_SOURCE_DIR": str(Path(tmp, "sources")),
            }, clear=False):
                app = AppTest.from_file(str(APP_PATH)).run(timeout=10)
                approve = next(button for button in app.button if "Approve & next" in button.label)
                self.assertFalse(approve.disabled)
                recipe_selector = next(item for item in app.selectbox if item.label == "Recipe")
                recipe_selector.set_value("Cranberry Date Bars").run(timeout=10)
                approve = next(button for button in app.button if "Approve & next" in button.label)
                self.assertTrue(approve.disabled)


if __name__ == "__main__":
    unittest.main()
