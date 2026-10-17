import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from tests.ui_fixture_factory import write_ui_fixture


APP_PATH = Path(__file__).resolve().parents[1] / "streamlit_app.py"


class StreamlitStructuredReviewTests(unittest.TestCase):
    def _app(self, root: str) -> AppTest:
        fixture = write_ui_fixture(Path(root))
        self.fixture = fixture
        self.env = patch.dict(os.environ, {
            "RECIPE_IMPORTER_OUTPUT_DIR": str(fixture["output"]),
            "RECIPE_IMPORTER_SOURCE_DIR": str(Path(root, "sources")),
        }, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        return AppTest.from_file(str(APP_PATH)).run(timeout=10)

    def test_recipe_editor_has_direct_section_tabs_and_row_controls(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = self._app(tmp)
            self.assertEqual([item.label for item in app.get("tab")], ["Ingredients", "Instructions"])
            labels = [button.label for button in app.button]
            self.assertIn("Add ingredient", labels)
            candidate_id = app.session_state["review_selection"]
            app.session_state[f"recipe_sections:{candidate_id}"] = "Instructions"
            app.run(timeout=10)
            labels = [button.label for button in app.button]
            self.assertIn("Add step", labels)
            self.assertTrue(any(item.label == "Actions" for item in app.menu_button))

    def test_suggestion_is_inline_and_approval_gate_remains_separate(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = self._app(tmp)
            recipe_selector = next(item for item in app.selectbox if item.label == "Jump to recipe")
            recipe_selector.set_value("Cranberry & Caramel Date Bars").run(timeout=10)
            visible = "\n".join(item.value for item in [*app.markdown, *app.caption, *app.text] if hasattr(item, "value"))
            self.assertIn("Unverified proposal", visible)
            self.assertTrue(any("Use suggestion" in button.label for button in app.button))
            self.assertTrue(any("Approve & next" in button.label for button in app.button))

            approve = next(button for button in app.button if "Approve & next" in button.label)
            self.assertTrue(approve.disabled)
            captions = [item.value for item in app.caption if hasattr(item, "value")]
            self.assertTrue(any("critical" in value.casefold() or "proposal" in value.casefold() for value in captions))


if __name__ == "__main__":
    unittest.main()
