import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from tests.ui_fixture_factory import write_ui_fixture
from import_recipes import _ocr_text_paths

APP_PATH = Path(__file__).resolve().parents[1] / "streamlit_app.py"


class StreamlitReviewTests(unittest.TestCase):
    def test_review_shows_hash_bound_cached_text_with_unavailable_source_notice(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = write_ui_fixture(Path(tmp))
            source = Path(fixture["source"])
            source.unlink()
            cache = _ocr_text_paths(Path(fixture["output"]), str(fixture["digest"]))["selected"]
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(
                "--- OCR PAGE 1 ---\nCached recipe one\n\n--- OCR PAGE 2 ---\nCached recipe two\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {
                "RECIPE_IMPORTER_OUTPUT_DIR": str(fixture["output"]),
                "RECIPE_IMPORTER_SOURCE_DIR": str(Path(tmp, "sources")),
            }, clear=False):
                app = AppTest.from_file(str(APP_PATH)).run(timeout=10)
                app = next(item for item in app.selectbox if item.label == "Jump to recipe").set_value(
                    "Fried Rice").run(timeout=10)

                first_text = "\n".join(str(item.value) for item in app.get("text"))
                self.assertIn("Cached recipe one", first_text)
                self.assertNotIn("Cached recipe two", first_text)
                self.assertTrue(any("Original source is unavailable" in item.value for item in app.warning))

                app = next(item for item in app.selectbox if item.label == "Source text page").set_value(2).run(timeout=10)
                second_text = "\n".join(str(item.value) for item in app.get("text"))

        self.assertFalse(app.exception)
        self.assertIn("Cached recipe two", second_text)
        self.assertNotIn("Cached recipe one", second_text)

    def test_review_source_text_tracks_page_and_does_not_change_review_or_import_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = write_ui_fixture(Path(tmp))
            output = Path(fixture["output"])
            review_path = output / "review_events.jsonl"
            import_path = output / "import_state.jsonl"
            review_before = review_path.read_bytes()
            import_before = import_path.read_bytes() if import_path.exists() else None
            with patch.dict(os.environ, {
                "RECIPE_IMPORTER_OUTPUT_DIR": str(output),
                "RECIPE_IMPORTER_SOURCE_DIR": str(Path(tmp, "sources")),
            }, clear=False):
                app = AppTest.from_file(str(APP_PATH)).run(timeout=10)
                app = next(item for item in app.selectbox if item.label == "Jump to recipe").set_value(
                    "Fried Rice").run(timeout=10)

                self.assertTrue(any(item.value == "Source text" for item in app.get("subheader")))
                first_text = "\n".join(str(item.value) for item in app.get("text"))
                self.assertIn("Fried Rice", first_text)
                self.assertNotIn("The Best Caramel Apples", first_text)

                app = next(item for item in app.selectbox if item.label == "Page").set_value(2).run(timeout=10)
                second_text = "\n".join(str(item.value) for item in app.get("text"))
            self.assertFalse(app.exception)
            self.assertIn("The Best Caramel Apples", second_text)
            self.assertNotIn("Fried Rice", second_text)
            self.assertEqual(review_path.read_bytes(), review_before)
            self.assertEqual(import_path.read_bytes() if import_path.exists() else None, import_before)

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

    def test_narrow_workspace_switch_preserves_recipe_and_source_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = write_ui_fixture(Path(tmp))
            with patch.dict(os.environ, {
                "RECIPE_IMPORTER_OUTPUT_DIR": str(fixture["output"]),
                "RECIPE_IMPORTER_SOURCE_DIR": str(Path(tmp, "sources")),
            }, clear=False):
                app = AppTest.from_file(str(APP_PATH)).run(timeout=10)
                app = next(item for item in app.selectbox if item.label == "Jump to recipe").set_value(
                    "Fried Rice").run(timeout=10)
                ingredient = next(item for item in app.text_input if item.label == "Ingredient 1")
                app = ingredient.set_value("3 cups freshly edited rice").run(timeout=10)
                app = next(item for item in app.selectbox if item.label == "Page").set_value(2).run(timeout=10)
                app = next(item for item in app.selectbox if item.label == "Source view").set_value("Inspect").run(timeout=10)

                app = app.get("segmented_control")[0].set_value("Recipe").run(timeout=10)
                self.assertEqual(app.session_state["review_workspace_view"], "Recipe")
                self.assertEqual(app.session_state["review_buffers"][app.session_state["review_selection"]]
                                 ["structured_ingredients"][0], "3 cups freshly edited rice")
                self.assertTrue(any(item.label == "Ingredient 1" and item.value == "3 cups freshly edited rice"
                                    for item in app.text_input))

                app = app.get("segmented_control")[0].set_value("Source").run(timeout=10)
                self.assertEqual(app.session_state["review_workspace_view"], "Source")
                candidate_id = app.session_state["review_selection"]

        self.assertFalse(app.exception)
        self.assertEqual(app.session_state[f"page:{candidate_id}"], 2)
        self.assertEqual(app.session_state[f"zoom:{candidate_id}"], "Inspect")
        self.assertEqual(app.session_state[f"recipe_sections:{candidate_id}"], "Ingredients")

    def test_header_approve_saves_the_latest_editor_value_as_approved_revision(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = write_ui_fixture(Path(tmp))
            with patch.dict(os.environ, {
                "RECIPE_IMPORTER_OUTPUT_DIR": str(fixture["output"]),
                "RECIPE_IMPORTER_SOURCE_DIR": str(Path(tmp, "sources")),
            }, clear=False):
                app = AppTest.from_file(str(APP_PATH)).run(timeout=10)
                app = next(item for item in app.selectbox if item.label == "Jump to recipe").set_value(
                    "Fried Rice").run(timeout=10)
                app = next(item for item in app.text_input if item.label == "Ingredient 1").set_value(
                    "4 cups latest rice edit").run(timeout=10)
                app = next(item for item in app.button if item.label == "Approve & next").click().run(timeout=10)

            events_path = Path(fixture["output"]) / "review_events.jsonl"
            events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
            revision = next(event for event in events if event.get("event") == "review_revision"
                            and event.get("recipe", {}).get("name") == "Fried Rice")
            approval = next(event for event in events if event.get("event") == "review_approval"
                            and event.get("candidate_id") == revision["candidate_id"])

        self.assertFalse(app.exception)
        self.assertEqual(revision["recipe"]["ingredients"][0], "4 cups latest rice edit")
        self.assertEqual(approval["revision"], revision["revision"])

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
