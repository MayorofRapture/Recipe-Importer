"""Regression tests for durable row identity and review widget buffers."""
from __future__ import annotations

import os
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from tests.ui_fixture_factory import write_ui_fixture
from app_pages.common import switch_buffer_mode
from ui_review_state import row_ids_for
from review_store import ReviewStore
from ui_data import load_catalog
from import_recipes import candidate_identity
from recipe_quality import ExtractionBatch, RecipeCandidate, Suggestion
from review_store import extraction_identity


APP_PATH = Path(__file__).resolve().parents[1] / "streamlit_app.py"


class ReviewBufferTests(unittest.TestCase):
    def test_jump_and_filter_each_reuse_the_page_catalog(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = write_ui_fixture(Path(tmp))
            env = patch.dict(os.environ, {
                "RECIPE_IMPORTER_OUTPUT_DIR": str(fixture["output"]),
                "RECIPE_IMPORTER_SOURCE_DIR": str(Path(tmp, "sources")),
            }, clear=False)
            env.start()
            self.addCleanup(env.stop)
            with patch("app_pages.common.load_catalog", wraps=load_catalog) as catalog_loader:
                app = AppTest.from_file(str(APP_PATH)).run(timeout=10)
                next(item for item in app.selectbox if item.label == "Show").set_value("All recipes").run(timeout=10)
                selector = next(item for item in app.selectbox if item.label == "Jump to recipe")
                before = catalog_loader.call_count
                selector.set_value("Fried Rice").run(timeout=10)
                self.assertEqual(catalog_loader.call_count - before, 1)
                self.assertEqual(app.session_state["review_selection"], app.session_state["review_active_selection"])

                before = catalog_loader.call_count
                next(item for item in app.text_input if item.label == "Search title or source").set_value("Cranberry").run(timeout=10)
                self.assertEqual(catalog_loader.call_count - before, 1)
                self.assertFalse(app.exception)

    def test_mode_switch_preserves_unapplied_raw_bulk_text_exactly(self):
        raw_ingredients = "  1 cup flour  \n\n??"
        buffer = {
            "mode": "quick", "raw_ingredients": raw_ingredients,
            "raw_instructions": "Mix.\n\nBake.",
        }

        switch_buffer_mode(buffer, "structured")
        switch_buffer_mode(buffer, "quick")

        self.assertEqual(buffer["raw_ingredients"], raw_ingredients)
        self.assertEqual(buffer["raw_instructions"], "Mix.\n\nBake.")

    def test_saved_row_ids_restore_after_reorder_and_include_inserted_rows(self):
        registration = [
            {"target_kind": "row", "field": "ingredients", "target_id": "source-a", "original_position": 0},
            {"target_kind": "row", "field": "ingredients", "target_id": "source-b", "original_position": 1},
        ]
        revision = [
            {"target_id": "source-b", "field": "ingredients", "position": 0},
            {"target_id": "new-row", "field": "ingredients", "position": 1},
            {"target_id": "source-a", "field": "ingredients", "position": 2},
        ]

        ids = row_ids_for("ingredients", 3, registration, revision)

        self.assertEqual(ids, ["source-b", "new-row", "source-a"])

    def test_widget_edit_is_captured_before_move_rerun(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = write_ui_fixture(Path(tmp))
            env = patch.dict(os.environ, {
                "RECIPE_IMPORTER_OUTPUT_DIR": str(fixture["output"]),
                "RECIPE_IMPORTER_SOURCE_DIR": str(Path(tmp, "sources")),
            }, clear=False)
            env.start()
            self.addCleanup(env.stop)
            app = AppTest.from_file(str(APP_PATH)).run(timeout=10)
            queue = next(item for item in app.selectbox if item.label == "Show")
            queue.set_value("All recipes").run(timeout=10)
            selector = next(item for item in app.selectbox if item.label == "Jump to recipe")
            selector.set_value("Fried Rice").run(timeout=10)

            row = next(item for item in app.text_input if item.label == "Ingredient 1")
            row.set_value("2 cups cold rice")
            move = next(item for item in app.menu_button if item.label == "Actions")
            move.set_value("Move down").run(timeout=10)

            self.assertFalse(app.exception)
            ingredients = {item.label: item.value for item in app.text_input if item.label.startswith("Ingredient ")}
            self.assertEqual(ingredients["Ingredient 1"], "1 egg")
            self.assertEqual(ingredients["Ingredient 2"], "2 cups cold rice")
            buffer = app.session_state["review_buffers"][app.session_state["review_selection"]]
            self.assertEqual(buffer["structured_ingredients"], ["1 egg", "2 cups cold rice"])
            expected_ids = list(buffer["ingredient_ids"])
            next(item for item in app.button if item.label == "Save draft").click().run(timeout=10)

            resumed = AppTest.from_file(str(APP_PATH)).run(timeout=10)
            restored = resumed.session_state["review_buffers"][resumed.session_state["review_selection"]]
            self.assertEqual(restored["ingredient_ids"], expected_ids)
            self.assertEqual(restored["structured_ingredients"], ["1 egg", "2 cups cold rice"])

    def test_all_proposals_remain_separate_and_keep_extraction_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = write_ui_fixture(Path(tmp))
            env = patch.dict(os.environ, {
                "RECIPE_IMPORTER_OUTPUT_DIR": str(fixture["output"]),
                "RECIPE_IMPORTER_SOURCE_DIR": str(Path(tmp, "sources")),
            }, clear=False)
            env.start()
            self.addCleanup(env.stop)
            app = AppTest.from_file(str(APP_PATH)).run(timeout=10)
            queue = next(item for item in app.selectbox if item.label == "Show")
            queue.set_value("All recipes").run(timeout=10)
            selector = next(item for item in app.selectbox if item.label == "Jump to recipe")
            selector.set_value("Cranberry & Caramel Date Bars").run(timeout=10)

            keys = [item.key for item in app.button if item.label == "Edit suggestion"]
            self.assertEqual(len(keys), 3)
            self.assertEqual(len(set(keys)), 3)
            self.assertFalse(app.exception)

    def test_save_conflict_keeps_buffer_and_blocks_selection_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = write_ui_fixture(Path(tmp))
            env = patch.dict(os.environ, {
                "RECIPE_IMPORTER_OUTPUT_DIR": str(fixture["output"]),
                "RECIPE_IMPORTER_SOURCE_DIR": str(Path(tmp, "sources")),
            }, clear=False)
            env.start()
            self.addCleanup(env.stop)
            with patch("app_pages.common.load_catalog", wraps=load_catalog) as catalog_loader:
                app = AppTest.from_file(str(APP_PATH)).run(timeout=10)
                next(item for item in app.selectbox if item.label == "Show").set_value("All recipes").run(timeout=10)
                selector = next(item for item in app.selectbox if item.label == "Jump to recipe")
                selector.set_value("Fried Rice").run(timeout=10)
                next(item for item in app.text_input if item.label == "Ingredient 1").set_value("2 cups cold rice").run(timeout=10)
                before = catalog_loader.call_count

                with patch.object(ReviewStore, "save_revision", side_effect=RuntimeError("revision conflict")):
                    next(item for item in app.selectbox if item.label == "Jump to recipe").set_value(
                        "Cranberry & Caramel Date Bars").run(timeout=10)
                self.assertEqual(catalog_loader.call_count - before, 1)

            self.assertFalse(app.exception)
            self.assertEqual(app.session_state["review_selection"], app.session_state["review_active_selection"])
            self.assertEqual(app.session_state["review_buffers"][app.session_state["review_selection"]]
                             ["structured_ingredients"][0], "2 cups cold rice")
            self.assertTrue(any("revision conflict" in item.value for item in app.error))

    def test_skip_flush_conflict_keeps_selection_and_unsaved_edit(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = write_ui_fixture(Path(tmp))
            env = patch.dict(os.environ, {
                "RECIPE_IMPORTER_OUTPUT_DIR": str(fixture["output"]),
                "RECIPE_IMPORTER_SOURCE_DIR": str(Path(tmp, "sources")),
            }, clear=False)
            env.start()
            self.addCleanup(env.stop)
            app = AppTest.from_file(str(APP_PATH)).run(timeout=10)
            candidate_id = app.session_state["review_selection"]
            next(item for item in app.text_input if item.label == "Ingredient 1").set_value(
                "2 cups retained rice").run(timeout=10)

            with patch.object(ReviewStore, "save_revision", side_effect=RuntimeError("revision conflict")):
                next(item for item in app.button if item.label == "Skip for now").click().run(timeout=10)

            self.assertFalse(app.exception)
            self.assertEqual(app.session_state["review_selection"], candidate_id)
            self.assertEqual(app.session_state["review_active_selection"], candidate_id)
            self.assertEqual(app.session_state["review_buffers"][candidate_id]
                             ["structured_ingredients"][0], "2 cups retained rice")
            self.assertTrue(any("revision conflict" in item.value for item in app.error))

    def test_save_conflict_blocks_queue_filter_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = write_ui_fixture(Path(tmp))
            env = patch.dict(os.environ, {
                "RECIPE_IMPORTER_OUTPUT_DIR": str(fixture["output"]),
                "RECIPE_IMPORTER_SOURCE_DIR": str(Path(tmp, "sources")),
            }, clear=False)
            env.start()
            self.addCleanup(env.stop)
            with patch("app_pages.common.load_catalog", wraps=load_catalog) as catalog_loader:
                app = AppTest.from_file(str(APP_PATH)).run(timeout=10)
                next(item for item in app.selectbox if item.label == "Show").set_value("All recipes").run(timeout=10)
                next(item for item in app.selectbox if item.label == "Jump to recipe").set_value("Fried Rice").run(timeout=10)
                next(item for item in app.text_input if item.label == "Ingredient 1").set_value("2 cups cold rice").run(timeout=10)
                before = catalog_loader.call_count

                with patch.object(ReviewStore, "save_revision", side_effect=RuntimeError("revision conflict")):
                    next(item for item in app.text_input if item.label == "Search title or source").set_value("Cranberry").run(timeout=10)
                self.assertEqual(catalog_loader.call_count - before, 1)

            self.assertFalse(app.exception)
            self.assertEqual(app.session_state["review_search"], "")
            self.assertEqual(app.session_state["review_selection"], app.session_state["review_active_selection"])
            self.assertEqual(app.session_state["review_buffers"][app.session_state["review_selection"]]
                             ["structured_ingredients"][0], "2 cups cold rice")

    def test_entrypoint_page_exit_guard_keeps_review_open_when_save_conflicts(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = write_ui_fixture(Path(tmp))
            env = patch.dict(os.environ, {
                "RECIPE_IMPORTER_OUTPUT_DIR": str(fixture["output"]),
                "RECIPE_IMPORTER_SOURCE_DIR": str(Path(tmp, "sources")),
            }, clear=False)
            env.start()
            self.addCleanup(env.stop)
            app = AppTest.from_file(str(APP_PATH)).run(timeout=10)
            next(item for item in app.selectbox if item.label == "Show").set_value("All recipes").run(timeout=10)
            next(item for item in app.selectbox if item.label == "Jump to recipe").set_value("Fried Rice").run(timeout=10)
            next(item for item in app.text_input if item.label == "Ingredient 1").set_value("2 cups cold rice").run(timeout=10)

            with patch.object(ReviewStore, "save_revision", side_effect=RuntimeError("revision conflict")):
                app.switch_page("app_pages/upload.py").run(timeout=10)

            self.assertFalse(app.exception)
            self.assertTrue(any(item.label == "Recipe title" for item in app.text_input))
            self.assertEqual(app.session_state["review_selection"], app.session_state["review_active_selection"])
            self.assertTrue(any("revision conflict" in item.value for item in app.error))

    def test_approval_failure_keeps_exact_recipe_selected(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = write_ui_fixture(Path(tmp))
            env = patch.dict(os.environ, {
                "RECIPE_IMPORTER_OUTPUT_DIR": str(fixture["output"]),
                "RECIPE_IMPORTER_SOURCE_DIR": str(Path(tmp, "sources")),
            }, clear=False)
            env.start()
            self.addCleanup(env.stop)
            app = AppTest.from_file(str(APP_PATH)).run(timeout=10)
            next(item for item in app.selectbox if item.label == "Show").set_value("All recipes").run(timeout=10)
            next(item for item in app.selectbox if item.label == "Jump to recipe").set_value("Fried Rice").run(timeout=10)

            with patch.object(ReviewStore, "approve_revision", side_effect=RuntimeError("approval write failed")):
                next(item for item in app.button if "Approve & next" in item.label).click().run(timeout=10)

            self.assertFalse(app.exception)
            self.assertEqual(app.session_state["review_selection"], app.session_state["review_active_selection"])
            self.assertIn(app.session_state["review_selection"], app.session_state["review_buffers"])
            self.assertTrue(any("approval was not recorded" in item.value.casefold() for item in app.error))

    def test_multiple_proposal_decisions_and_approval_share_one_exact_revision(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = write_ui_fixture(Path(tmp))
            _replace_fixture_candidates_with_independent_proposals(fixture)
            env = patch.dict(os.environ, {
                "RECIPE_IMPORTER_OUTPUT_DIR": str(fixture["output"]),
                "RECIPE_IMPORTER_SOURCE_DIR": str(Path(tmp, "sources")),
            }, clear=False)
            env.start()
            self.addCleanup(env.stop)
            app = AppTest.from_file(str(APP_PATH)).run(timeout=10)
            next(item for item in app.selectbox if item.label == "Show").set_value("All recipes").run(timeout=10)
            next(item for item in app.selectbox if item.label == "Jump to recipe").set_value("Two Proposal Sample").run(timeout=10)
            candidate_id = app.session_state["review_selection"]

            next(item for item in app.button if item.key == f"approve_s:{candidate_id}:quantity-proposal").click().run(timeout=10)
            next(item for item in app.button if item.key == f"approve_s:{candidate_id}:yield-proposal").click().run(timeout=10)

            approve = next(item for item in app.button if item.key == f"approve:{candidate_id}")
            self.assertFalse(approve.disabled, [item.value for item in app.caption])
            approve.click().run(timeout=10)

            views = load_catalog(Path(fixture["output"]), Path(tmp, "sources"))
            view = next(item for item in views if item.candidate_id == candidate_id)
            self.assertEqual(view.revision, 1)
            self.assertEqual(view.status, "Ready to import")

    def test_approval_does_not_let_queue_flush_write_after_the_approved_revision(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = write_ui_fixture(Path(tmp))
            output = Path(fixture["output"])
            env = patch.dict(os.environ, {
                "RECIPE_IMPORTER_OUTPUT_DIR": str(output),
                "RECIPE_IMPORTER_SOURCE_DIR": str(Path(tmp, "sources")),
            }, clear=False)
            env.start()
            self.addCleanup(env.stop)
            app = AppTest.from_file(str(APP_PATH)).run(timeout=10)
            next(item for item in app.selectbox if item.label == "Show").set_value("All recipes").run(timeout=10)
            next(item for item in app.selectbox if item.label == "Jump to recipe").set_value(
                "The Best Caramel Apples").run(timeout=10)
            candidate_id = app.session_state["review_selection"]
            self.assertTrue(any("review" in item.value.casefold() for item in app.caption if hasattr(item, "value")))

            next(item for item in app.button if item.key == f"save:{candidate_id}").click().run(timeout=10)
            next(item for item in app.button if item.key == f"approve:{candidate_id}").click().run(timeout=10)

            events = [json.loads(line) for line in
                      (output / "review_events.jsonl").read_text(encoding="utf-8").splitlines()]
            revisions = [item for item in events
                         if item.get("event") == "review_revision" and item.get("candidate_id") == candidate_id]
            approvals = [item for item in events
                         if item.get("event") == "review_approval" and item.get("candidate_id") == candidate_id]
            view = next(item for item in load_catalog(output) if item.candidate_id == candidate_id)

        self.assertFalse(app.exception)
        self.assertEqual(len(revisions), 1)
        self.assertEqual(len(approvals), 1)
        self.assertEqual(approvals[0]["revision"], revisions[0]["revision"])
        self.assertTrue(view.import_ready)


def _replace_fixture_candidates_with_independent_proposals(fixture) -> None:
    source = Path(fixture["source"])
    digest = str(fixture["digest"])
    output = Path(fixture["output"])
    candidate = RecipeCandidate(
        name="Two Proposal Sample",
        ingredients=["flour"],
        instructions=["Bake."],
        suggestions=[
            Suggestion(suggestion_id="quantity-proposal", field_path="ingredients[0]",
                       proposed_value="2 cups flour", rationale="Plausible amount.", uncertainty="Not printed."),
            Suggestion(suggestion_id="yield-proposal", field_path="yield_text",
                       proposed_value="12 cookies", rationale="Plausible yield.", uncertainty="Not printed."),
        ],
    )
    batch = ExtractionBatch(recipes=[candidate])
    (output / "extracted" / f"{digest}.json").write_text(json.dumps({
        "schema_version": 2, "source_hash": digest, "batch": batch.model_dump(mode="json"),
    }, ensure_ascii=False), encoding="utf-8")
    (output / "review_events.jsonl").write_text("", encoding="utf-8")
    store = ReviewStore(output / "review_events.jsonl")
    candidate_id = candidate_identity(digest, candidate)
    store.add_candidate(str(source), candidate_id, candidate)
    store.register_candidates(str(source), digest,
                              extraction_identity(digest, batch.model_dump(mode="json")),
                              batch.recipes)


if __name__ == "__main__":
    unittest.main()
