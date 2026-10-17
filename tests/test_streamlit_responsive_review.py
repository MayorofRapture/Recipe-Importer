import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from tests.ui_fixture_factory import write_ui_fixture
from ui_data import load_catalog


APP_PATH = Path(__file__).resolve().parents[1] / "streamlit_app.py"
REVIEW_PATH = APP_PATH.parent / "app_pages" / "review.py"


class StreamlitResponsiveReviewTests(unittest.TestCase):
    def test_review_has_compact_queue_and_editable_metadata_without_fixed_scroll_regions(self):
        source = REVIEW_PATH.read_text(encoding="utf-8")

        self.assertIn("Review queue", source)
        self.assertIn("Source file", source)
        self.assertIn("Yield", source)
        self.assertIn("Description", source)
        self.assertNotIn("height=650", source)
        self.assertIn("alt=", source)

    def test_bulk_mode_offers_explicit_apply_cancel_and_preview(self):
        source = REVIEW_PATH.read_text(encoding="utf-8")
        self.assertIn('f"Apply {field}"', source)
        self.assertIn("Cancel bulk edit", source)
        self.assertIn("Preview:", source)

    def test_approve_action_is_enabled_for_ready_recipe_and_blocked_for_critical_recipe(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = write_ui_fixture(Path(tmp))
            with patch.dict(os.environ, {
                "RECIPE_IMPORTER_OUTPUT_DIR": str(fixture["output"]),
                "RECIPE_IMPORTER_SOURCE_DIR": str(Path(tmp, "sources")),
            }, clear=False):
                app = AppTest.from_file(str(REVIEW_PATH)).run(timeout=10)
                approve = next(button for button in app.button if "Approve & next" in button.label)
                self.assertFalse(approve.disabled)
                recipe_selector = next(item for item in app.selectbox if item.label == "Jump to recipe")
                recipe_selector.set_value("Cranberry & Caramel Date Bars").run(timeout=10)
                approve = next(button for button in app.button if "Approve & next" in button.label)
                self.assertTrue(approve.disabled)

    def test_title_and_optional_metadata_commit_to_a_draft_revision(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = write_ui_fixture(Path(tmp))
            with patch.dict(os.environ, {
                "RECIPE_IMPORTER_OUTPUT_DIR": str(fixture["output"]),
                "RECIPE_IMPORTER_SOURCE_DIR": str(Path(tmp, "sources")),
            }, clear=False):
                app = AppTest.from_file(str(APP_PATH))
                app.session_state["review_show_optional_details"] = True
                app.run(timeout=10)
                next(item for item in app.text_input if item.label == "Recipe title").set_value("Rice for Two").run(timeout=10)
                yield_widget = next(item for item in app.text_input if item.label == "Yield")
                yield_widget.set_value("2 servings").run(timeout=10)
                self.assertFalse(app.exception)
                views = load_catalog(Path(fixture["output"]), Path(tmp, "sources"))
                fried = next(item for item in views if item.candidate.name == "Rice for Two")
                self.assertEqual(fried.candidate.yield_text, "2 servings")
                self.assertGreaterEqual(fried.revision, 1)

    def test_bulk_text_is_not_applied_until_explicit_apply(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = write_ui_fixture(Path(tmp))
            with patch.dict(os.environ, {
                "RECIPE_IMPORTER_OUTPUT_DIR": str(fixture["output"]),
                "RECIPE_IMPORTER_SOURCE_DIR": str(Path(tmp, "sources")),
            }, clear=False):
                app = AppTest.from_file(str(APP_PATH)).run(timeout=10)
                candidate_id = app.session_state["review_selection"]
                app.session_state[f"recipe_sections:{candidate_id}"] = "Ingredients"
                next(item for item in app.button if item.label == "Bulk edit ingredients").click().run(timeout=10)
                bulk = next(item for item in app.text_area if item.label == "Bulk ingredients")
                bulk.set_value("1 cup rice\n1 egg").run(timeout=10)
                buffer = app.session_state["review_buffers"][candidate_id]
                self.assertEqual(buffer["structured_ingredients"], ["2 cups rice", "1 egg"])
                next(item for item in app.button if item.label == "Apply ingredients").click().run(timeout=10)
                self.assertEqual(app.session_state["review_buffers"][candidate_id]["structured_ingredients"],
                                 ["1 cup rice", "1 egg"])

    def test_raw_bulk_text_survives_save_and_fresh_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = write_ui_fixture(Path(tmp))
            env = patch.dict(os.environ, {
                "RECIPE_IMPORTER_OUTPUT_DIR": str(fixture["output"]),
                "RECIPE_IMPORTER_SOURCE_DIR": str(Path(tmp, "sources")),
            }, clear=False)
            env.start()
            self.addCleanup(env.stop)
            app = AppTest.from_file(str(REVIEW_PATH)).run(timeout=10)
            candidate_id = app.session_state["review_selection"]
            app.session_state[f"recipe_sections:{candidate_id}"] = "Ingredients"
            next(item for item in app.button if item.label == "Bulk edit ingredients").click().run(timeout=10)
            raw = "  1 cup rice  \n\n??"
            next(item for item in app.text_area if item.label == "Bulk ingredients").set_value(raw).run(timeout=10)
            next(item for item in app.button if item.label == "Save draft").click().run(timeout=10)
            next_app = AppTest.from_file(str(REVIEW_PATH)).run(timeout=10)
            next_app.session_state[f"recipe_sections:{candidate_id}"] = "Ingredients"
            next(item for item in next_app.button if item.label == "Bulk edit ingredients").click().run(timeout=10)
            self.assertEqual(next(item for item in next_app.text_area if item.label == "Bulk ingredients").value, raw)
            self.assertEqual(next_app.session_state["review_selection"], candidate_id)

    def test_malformed_local_catalog_blocks_mutations_and_preserves_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = write_ui_fixture(Path(tmp))
            ledger = Path(fixture["output"]) / "review_events.jsonl"
            ledger.write_text('{malformed\n', encoding="utf-8")
            with patch.dict(os.environ, {
                "RECIPE_IMPORTER_OUTPUT_DIR": str(fixture["output"]),
                "RECIPE_IMPORTER_SOURCE_DIR": str(Path(tmp, "sources")),
            }, clear=False):
                app = AppTest.from_file(str(APP_PATH)).run(timeout=10)
            self.assertFalse(app.exception)
            self.assertTrue(any("history is preserved" in item.value for item in app.error))
            self.assertFalse(any(item.label == "Save draft" for item in app.button))
            self.assertEqual(ledger.read_text(encoding="utf-8"), "{malformed\n")

    def test_source_page_selector_uses_real_pdf_page_count_and_queue_hides_hashes(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = write_ui_fixture(Path(tmp))
            with patch.dict(os.environ, {
                "RECIPE_IMPORTER_OUTPUT_DIR": str(fixture["output"]),
                "RECIPE_IMPORTER_SOURCE_DIR": str(Path(tmp, "sources")),
            }, clear=False):
                app = AppTest.from_file(str(REVIEW_PATH)).run(timeout=10)
                page = next(item for item in app.selectbox if item.label == "Page")
                self.assertEqual(page.options, ["1 of 2", "2 of 2"])
                self.assertIn("Source view", [item.label for item in app.selectbox])
                page.set_value(2).run(timeout=10)
                self.assertEqual(app.session_state[page.key], 2)
                queue = next(item for item in app.selectbox if item.label == "Show")
                queue.set_value("All recipes").run(timeout=10)
                source_filter = next(item for item in app.selectbox if item.label == "Source file")
                self.assertIn("cookbook.pdf", source_filter.options)
                self.assertNotIn(str(fixture["digest"]), source_filter.options)


if __name__ == "__main__":
    unittest.main()
