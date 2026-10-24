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
                next(item for item in app.selectbox if item.label == "Jump to recipe").set_value("Fried Rice").run(timeout=10)
                next(item for item in app.text_input if item.label == "Ingredient 1").set_value("2 cups cold rice").run(timeout=10)
                app = next(button for button in app.button if button.label == "Save draft").click().run(timeout=10)

            review_path = Path(fixture["output"]) / "review_events.jsonl"
            events = [json.loads(line) for line in review_path.read_text(encoding="utf-8").splitlines()]
            revisions = [event for event in events if event.get("event") == "review_revision"
                         and event.get("recipe", {}).get("name") == "Fried Rice"]
            self.assertEqual(len(revisions), 1)
            self.assertEqual(revisions[0]["revision"], 1)
            self.assertFalse((Path(fixture["output"]) / "ui_drafts.jsonl").exists())
            self.assertTrue(any("Draft saved (revision 1)." in item.value
                                for item in app.caption if hasattr(item, "value")))
            status = app.session_state["review_save_state"]
            self.assertEqual(status["candidate_id"], app.session_state["review_selection"])
            self.assertEqual(status["revision"], 1)

    def test_save_confirmation_clears_when_recipe_is_edited_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = write_ui_fixture(Path(tmp))
            with patch.dict(os.environ, {
                "RECIPE_IMPORTER_OUTPUT_DIR": str(fixture["output"]),
                "RECIPE_IMPORTER_SOURCE_DIR": str(Path(tmp, "sources")),
            }, clear=False):
                app = AppTest.from_file(str(APP_PATH)).run(timeout=10)
                next(item for item in app.selectbox if item.label == "Jump to recipe").set_value("Fried Rice").run(timeout=10)
                next(item for item in app.text_input if item.label == "Ingredient 1").set_value("2 cups cold rice").run(timeout=10)
                app = next(button for button in app.button if button.label == "Save draft").click().run(timeout=10)
                self.assertTrue(any("Draft saved (revision 1)." in item.value
                                    for item in app.caption if hasattr(item, "value")))

                app = next(item for item in app.text_input if item.label == "Ingredient 1").set_value(
                    "3 cups cold rice").run(timeout=10)

            self.assertFalse(app.exception)
            self.assertFalse(any("Draft saved (revision 1)." in item.value
                                 for item in app.caption if hasattr(item, "value")))
            self.assertNotIn("review_save_state", app.session_state)
            self.assertEqual(app.session_state["review_buffers"][app.session_state["review_selection"]]
                             ["structured_ingredients"][0], "3 cups cold rice")

    def test_save_confirmation_does_not_leak_to_another_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = write_ui_fixture(Path(tmp))
            with patch.dict(os.environ, {
                "RECIPE_IMPORTER_OUTPUT_DIR": str(fixture["output"]),
                "RECIPE_IMPORTER_SOURCE_DIR": str(Path(tmp, "sources")),
            }, clear=False):
                app = AppTest.from_file(str(APP_PATH)).run(timeout=10)
                next(item for item in app.selectbox if item.label == "Jump to recipe").set_value("Fried Rice").run(timeout=10)
                next(item for item in app.text_input if item.label == "Ingredient 1").set_value("2 cups cold rice").run(timeout=10)
                app = next(button for button in app.button if button.label == "Save draft").click().run(timeout=10)
                app = next(item for item in app.selectbox if item.label == "Jump to recipe").set_value(
                    "Cranberry & Caramel Date Bars").run(timeout=10)

            self.assertFalse(app.exception)
            self.assertFalse(any("Draft saved (revision 1)." in item.value
                                 for item in app.caption if hasattr(item, "value")))
            self.assertNotIn("review_save_state", app.session_state)

    def test_noop_save_reports_existing_revision_without_appending_another(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = write_ui_fixture(Path(tmp))
            with patch.dict(os.environ, {
                "RECIPE_IMPORTER_OUTPUT_DIR": str(fixture["output"]),
                "RECIPE_IMPORTER_SOURCE_DIR": str(Path(tmp, "sources")),
            }, clear=False):
                app = AppTest.from_file(str(APP_PATH)).run(timeout=10)
                next(item for item in app.selectbox if item.label == "Jump to recipe").set_value("Fried Rice").run(timeout=10)
                next(item for item in app.text_input if item.label == "Ingredient 1").set_value("2 cups cold rice").run(timeout=10)
                app = next(button for button in app.button if button.label == "Save draft").click().run(timeout=10)
                app = next(button for button in app.button if button.label == "Save draft").click().run(timeout=10)

            review_path = Path(fixture["output"]) / "review_events.jsonl"
            events = [json.loads(line) for line in review_path.read_text(encoding="utf-8").splitlines()]
            revisions = [event for event in events if event.get("event") == "review_revision"
                         and event.get("recipe", {}).get("name") == "Fried Rice"]
            self.assertEqual(len(revisions), 1)
            self.assertTrue(any("Draft already saved (revision 1)." in item.value
                                for item in app.caption if hasattr(item, "value")))

    def test_failed_save_keeps_current_edits_and_reports_error(self):
        from review_store import ReviewStore

        with tempfile.TemporaryDirectory() as tmp:
            fixture = write_ui_fixture(Path(tmp))
            with patch.dict(os.environ, {
                "RECIPE_IMPORTER_OUTPUT_DIR": str(fixture["output"]),
                "RECIPE_IMPORTER_SOURCE_DIR": str(Path(tmp, "sources")),
            }, clear=False):
                app = AppTest.from_file(str(APP_PATH)).run(timeout=10)
                next(item for item in app.selectbox if item.label == "Jump to recipe").set_value("Fried Rice").run(timeout=10)
                next(item for item in app.text_input if item.label == "Ingredient 1").set_value("2 cups retained rice").run(timeout=10)
                with patch.object(ReviewStore, "save_revision", side_effect=RuntimeError("revision conflict")):
                    app = next(button for button in app.button if button.label == "Save draft").click().run(timeout=10)

            self.assertFalse(app.exception)
            self.assertTrue(any("Draft was not saved" in item.value and "revision conflict" in item.value
                                for item in app.error))
            self.assertEqual(app.session_state["review_buffers"][app.session_state["review_selection"]]
                             ["structured_ingredients"][0], "2 cups retained rice")


if __name__ == "__main__":
    unittest.main()
