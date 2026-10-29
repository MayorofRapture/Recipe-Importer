import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from tests.ui_fixture_factory import write_ui_fixture


APP_PATH = Path(__file__).resolve().parents[1] / "streamlit_app.py"


class StreamlitNavigationTests(unittest.TestCase):
    def test_alternate_library_warning_is_visible_and_paths_stay_in_troubleshooting(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = write_ui_fixture(Path(tmp))
            alternate = Path(tmp) / "other-library"
            alternate.mkdir()
            (alternate / "synthetic-marker.txt").write_text("synthetic", encoding="utf-8")
            with patch.dict(os.environ, {
                "RECIPE_IMPORTER_OUTPUT_DIR": str(fixture["output"]),
                "RECIPE_IMPORTER_SOURCE_DIR": str(Path(tmp, "sources")),
            }, clear=False), patch("library_paths.alternate_cwd_library", return_value=alternate):
                app = AppTest.from_file(str(APP_PATH)).run(timeout=10)

            self.assertFalse(app.exception)
            warning = next(item.value for item in app.warning
                           if "Another populated library exists" in item.value)
            self.assertIn("This app is using the selected library", warning)
            self.assertNotIn(str(alternate), warning)
            self.assertNotIn(str(fixture["output"]), warning)
            self.assertNotIn(str(alternate), app.caption[0].value)
            self.assertNotIn(str(fixture["output"]), app.caption[0].value)
            details = next(item for item in app.expander if item.label == "Library troubleshooting")
            detail_text = "\n".join(child.value for child in details.children.values())
            self.assertIn(str(alternate), detail_text)
            self.assertIn(str(fixture["output"]), detail_text)
            self.assertIn("Set RECIPE_IMPORTER_OUTPUT_DIR", detail_text)

    def test_alternate_library_warning_is_absent_without_a_conflict(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = write_ui_fixture(Path(tmp))
            with patch.dict(os.environ, {
                "RECIPE_IMPORTER_OUTPUT_DIR": str(fixture["output"]),
                "RECIPE_IMPORTER_SOURCE_DIR": str(Path(tmp, "sources")),
            }, clear=False), patch("library_paths.alternate_cwd_library", return_value=None):
                app = AppTest.from_file(str(APP_PATH)).run(timeout=10)

        self.assertFalse(app.exception)
        self.assertFalse(any("Another populated library exists" in item.value for item in app.warning))

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
        self.assertTrue(any("Fried Rice" in item.value for item in app.markdown))
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
        self.assertIn("Review pane", [item.label for item in app.get("segmented_control")])


if __name__ == "__main__":
    unittest.main()
