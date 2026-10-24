import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from tests.ui_fixture_factory import write_ui_fixture

APP = Path(__file__).resolve().parents[1] / "streamlit_app.py"


class EntrypointDefaultsTests(unittest.TestCase):
    def test_empty_installation_opens_upload_workflow(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"RECIPE_IMPORTER_OUTPUT_DIR": str(Path(tmp) / "output"),
                                         "RECIPE_IMPORTER_SOURCE_DIR": "",
                                         "RECIPE_IMPORTER_FIXTURE_MODE": "0"}):
                app = AppTest.from_file(str(APP)).run(timeout=15)
            self.assertFalse(app.exception)
            self.assertTrue(any("Upload" in item.value for item in app.title))
            self.assertFalse(any(item.label.startswith("Ingredient ") for item in app.text_input))
            self.assertTrue(app.session_state["initial_redirect_done"])

    def test_existing_library_redirects_from_stable_root_to_review_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = write_ui_fixture(Path(tmp))
            with patch.dict(os.environ, {
                "RECIPE_IMPORTER_OUTPUT_DIR": str(fixture["output"]),
                "RECIPE_IMPORTER_SOURCE_DIR": str(Path(tmp) / "sources"),
                "RECIPE_IMPORTER_FIXTURE_MODE": "1",
            }):
                app = AppTest.from_file(str(APP)).run(timeout=15)

        self.assertFalse(app.exception)
        self.assertEqual(app.session_state["initial_destination"], "Review")
        self.assertTrue(app.session_state["initial_redirect_done"])
        self.assertTrue(any(item.label == "Recipe title" for item in app.text_input))
