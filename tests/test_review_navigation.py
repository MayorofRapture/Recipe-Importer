from __future__ import annotations

import os
import json
import shutil
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from tests.ui_fixture_factory import write_ui_fixture
from ui_data import load_catalog
from recipe_quality import RecipeCandidate
from app_pages.common import buffer_for, paths as configured_paths, save_draft
from review_store import ReviewStore


APP_PATH = Path(__file__).resolve().parents[1] / "streamlit_app.py"


class ReviewNavigationTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parents[1] / f".test-review-navigation-{uuid.uuid4().hex}"
        self.root.mkdir()
        self.addCleanup(lambda: shutil.rmtree(self.root, ignore_errors=True))
        self.fixture = write_ui_fixture(self.root)
        production_output = (Path(__file__).resolve().parents[1] / "output").resolve()
        fixture_output = Path(self.fixture["output"]).resolve()
        self.assertNotEqual(fixture_output, production_output)
        self.env = patch.dict(os.environ, {
            "RECIPE_IMPORTER_OUTPUT_DIR": str(self.fixture["output"]),
            "RECIPE_IMPORTER_SOURCE_DIR": str(self.root / "sources"),
        }, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.assertEqual(configured_paths()[0].resolve(), fixture_output)
        self.assertEqual(configured_paths()[1].resolve(), (self.root / "sources").resolve())
        self.assertIn("synthetic", (fixture_output / ".fixture-library")
                      .read_text(encoding="utf-8").casefold())
        titles = {view.candidate.name for view in load_catalog(fixture_output, self.root / "sources")}
        self.assertTrue({"Fried Rice", "The Best Caramel Apples", "Cranberry & Caramel Date Bars"} <= titles)

    def test_skip_then_back_returns_to_prior_recipe(self):
        app = AppTest.from_file(str(APP_PATH)).run(timeout=10)
        recipe_a = app.session_state["review_selection"]
        next(item for item in app.text_input if item.label == "Ingredient 1").set_value(
            "2 cups saved before Back")
        next(item for item in app.button if item.label == "Skip for now").click().run(timeout=10)
        recipe_b = app.session_state["review_selection"]
        self.assertNotEqual(recipe_a, recipe_b)

        next(item for item in app.button if item.label == "Back").click().run(timeout=10)
        self.assertEqual(app.session_state["review_selection"], recipe_a)
        self.assertEqual(next(item for item in app.text_input if item.label == "Ingredient 1").value,
                         "2 cups saved before Back")
        self.assertFalse(app.exception)

    def test_skipped_list_persists_and_opens_without_catalog_reload(self):
        with patch("app_pages.common.load_catalog", wraps=load_catalog) as loader:
            app = AppTest.from_file(str(APP_PATH)).run(timeout=10)
            recipe_a = app.session_state["review_selection"]
            next(item for item in app.button if item.label == "Skip for now").click().run(timeout=10)
            before = loader.call_count
            next(item for item in app.button if item.label.startswith("Skipped recipes (")).click().run(timeout=10)
            self.assertEqual(loader.call_count, before)
            self.assertTrue(any(item.value == "Skipped recipes" for item in app.title))

            resumed = AppTest.from_file(str(APP_PATH)).run(timeout=10)
            next(item for item in resumed.button if item.label.startswith("Skipped recipes (")).click().run(timeout=10)
            self.assertTrue(any(item.label == f"Return to Fried Rice" for item in resumed.button))
            next(item for item in resumed.button if item.label == "Return to Fried Rice").click().run(timeout=10)
            self.assertEqual(resumed.session_state["review_selection"], recipe_a)
            self.assertFalse(resumed.exception)

    def test_returning_to_skipped_recipe_relaxes_filters_if_status_changed(self):
        app = AppTest.from_file(str(APP_PATH)).run(timeout=10)
        recipe_a = app.session_state["review_selection"]
        next(item for item in app.button if item.label == "Skip for now").click().run(timeout=10)

        view = next(item for item in load_catalog(Path(self.fixture["output"]), self.root / "sources")
                    if item.candidate_id == recipe_a)
        saved, _ = save_draft(view, buffer_for(view))
        store = ReviewStore(Path(self.fixture["output"]) / "review_events.jsonl")
        store.approve_revision(
            candidate_id=view.candidate_id, source_hash=view.source_hash,
            extraction_id=view.extraction_id, revision=saved.revision,
            operation_id=f"test-approve-now-ready:{view.candidate_id}",
            source_path=view.source_path,
        )

        next(item for item in app.button if item.label.startswith("Skipped recipes (")).click().run(timeout=10)
        next(item for item in app.button if item.label == "Return to Fried Rice").click().run(timeout=10)
        self.assertEqual(app.session_state["review_selection"], recipe_a)
        self.assertEqual(app.session_state["review_queue"], "All recipes")
        self.assertTrue(any("outside the saved queue filters" in item.value for item in app.info))
        self.assertFalse(app.exception)

    def test_skip_advances_across_the_25_recipe_page_boundary(self):
        cache = next((Path(self.fixture["output"]) / "extracted").glob("*.json"))
        payload = json.loads(cache.read_text(encoding="utf-8"))
        payload["batch"]["recipes"].extend(
            RecipeCandidate(name=f"Queue fixture {index:02d}", ingredients=["1 cup flour"],
                            instructions=["Mix and bake."]).model_dump(mode="json")
            for index in range(23)
        )
        cache.write_text(json.dumps(payload), encoding="utf-8")
        app = AppTest.from_file(str(APP_PATH)).run(timeout=10)
        next(item for item in app.selectbox if item.label == "Show").set_value("All recipes").run(timeout=10)
        all_ids = app.session_state["review_all_queue_ids"]
        self.assertEqual(len(all_ids), 27)
        jump = next(item for item in app.selectbox if item.label == "Jump to recipe")
        jump.set_value(jump.options[-1]).run(timeout=10)
        next(item for item in app.button if item.label == "Skip for now").click().run(timeout=10)
        self.assertEqual(app.session_state["review_page"], 2)
        self.assertEqual(app.session_state["review_selection"], all_ids[25])
        jump = next(item for item in app.selectbox if item.label == "Jump to recipe")
        jump.set_value(jump.options[-1]).run(timeout=10)
        next(item for item in app.button if item.label == "Skip for now").click().run(timeout=10)
        self.assertIsNone(app.session_state["review_selection"])
        self.assertTrue(any("reached the end of this queue" in item.value for item in app.success))
        self.assertTrue(any(item.label.startswith("Skipped recipes (") for item in app.button))
        next(item for item in app.button if item.key == "queue_end_back").click().run(timeout=10)
        self.assertEqual(app.session_state["review_selection"], all_ids[-1])
        self.assertFalse(app.exception)

    def test_back_remains_available_on_an_imported_read_only_recipe(self):
        app = AppTest.from_file(str(APP_PATH)).run(timeout=10)
        fried_rice = app.session_state["review_selection"]
        next(item for item in app.selectbox if item.label == "Show").set_value("All recipes").run(timeout=10)
        catalog = load_catalog(Path(self.fixture["output"]), self.root / "sources")
        imported_id = next(item.candidate_id for item in catalog if item.candidate.name == "Imported Recipe")
        next(item for item in app.selectbox if item.label == "Jump to recipe").set_value(imported_id).run(timeout=10)
        self.assertEqual(app.session_state["review_selection"], imported_id)
        self.assertTrue(any("Read-only" in item.value for item in app.warning))
        next(item for item in app.button if item.key == "approval_blocker:protected:0").click().run(timeout=10)
        self.assertEqual(app.session_state["review_selection"], imported_id)
        self.assertEqual(app.session_state["review_protected_target"], imported_id)
        self.assertTrue(any("protects the recipe from approval" in item.value for item in app.info))
        next(item for item in app.button if item.label == "Back").click().run(timeout=10)
        self.assertEqual(app.session_state["review_selection"], fried_rice)
        self.assertFalse(app.exception)

    def test_blocker_review_keeps_the_candidate_and_opens_its_destination(self):
        output = Path(self.fixture["output"])
        view = next(view for view in load_catalog(output) if view.candidate.name == "Fried Rice")
        candidate_id = view.candidate_id
        buffer = buffer_for(view)
        buffer["structured_instructions"] = []
        buffer["raw_instructions"] = ""
        save_draft(view, buffer)
        Path(self.fixture["source"]).unlink()

        app = AppTest.from_file(str(APP_PATH)).run(timeout=10)
        next(item for item in app.selectbox if item.label == "Show").set_value("All recipes").run(timeout=10)
        next(item for item in app.selectbox if item.label == "Jump to recipe").set_value(candidate_id).run(timeout=10)
        next(item for item in app.button if item.key == "approval_blocker:incomplete:1").click().run(timeout=10)
        self.assertEqual(app.session_state["review_selection"], candidate_id)
        self.assertEqual(app.session_state[f"recipe_sections:{candidate_id}"], "Instructions")
        next(item for item in app.button if item.key == "approval_blocker:source:0").click().run(timeout=10)
        self.assertEqual(app.session_state["review_selection"], candidate_id)
        self.assertEqual(app.session_state["review_workspace_view"], "Source")
        self.assertFalse(app.exception)

    def test_title_and_ingredient_blockers_open_the_matching_editor_area(self):
        output = Path(self.fixture["output"])
        view = next(item for item in load_catalog(output) if item.candidate.name == "Fried Rice")
        candidate_id = view.candidate_id
        buffer = buffer_for(view)
        buffer["name"] = ""
        buffer["structured_ingredients"] = []
        buffer["raw_ingredients"] = ""
        save_draft(view, buffer)

        app = AppTest.from_file(str(APP_PATH)).run(timeout=10)
        next(item for item in app.selectbox if item.label == "Show").set_value("All recipes").run(timeout=10)
        next(item for item in app.selectbox if item.label == "Jump to recipe").set_value(candidate_id).run(timeout=10)
        next(item for item in app.button if item.key == "approval_blocker:incomplete:0").click().run(timeout=10)
        self.assertEqual(app.session_state["review_selection"], candidate_id)
        self.assertTrue(any("Recipe title field below" in item.value for item in app.info))
        next(item for item in app.button if item.key == "approval_blocker:incomplete:1").click().run(timeout=10)
        self.assertEqual(app.session_state["review_selection"], candidate_id)
        self.assertEqual(app.session_state[f"recipe_sections:{candidate_id}"], "Ingredients")
        self.assertFalse(app.exception)

    def test_manual_no_instructions_and_source_unavailable_approval_workflow(self):
        output = Path(self.fixture["output"])
        source = Path(self.fixture["source"])
        view = next(item for item in load_catalog(output) if item.candidate.name == "Fried Rice")
        buffer = buffer_for(view)
        buffer["structured_instructions"] = []
        buffer["raw_instructions"] = ""
        save_draft(view, buffer)
        source.unlink()

        app = AppTest.from_file(str(APP_PATH)).run(timeout=10)
        next(item for item in app.selectbox if item.label == "Show").set_value("All recipes").run(timeout=10)
        next(item for item in app.selectbox if item.label == "Jump to recipe").set_value(view.candidate_id).run(timeout=10)
        app.radio(key=f"instruction_handling:{view.candidate_id}").set_value(
            "Confirm that no instructions are needed").run(timeout=10)
        next(item for item in app.button if item.key == f"confirm_no_instructions:{view.candidate_id}").click().run(timeout=10)
        current = next(item for item in load_catalog(output) if item.candidate_id == view.candidate_id)
        self.assertEqual(current.no_instructions_reason, "Dry ingredient blend")
        self.assertEqual(app.session_state["review_selection"], view.candidate_id)

        self.assertTrue(any("Saved revision" in item.value and "Fried Rice" in item.value
                            for item in app.markdown))
        ack = next(item for item in app.checkbox if item.label.startswith("I have reviewed the recipe"))
        app.checkbox(key=ack.key).check().run(timeout=10)
        rationale = next(item for item in app.text_area
                         if item.label == "Why are you approving this recipe without its original source?")
        app.text_area(key=rationale.key).set_value(
            "Reviewed the current recipe without the original scan.").run(timeout=10)
        next(item for item in app.button if item.key == f"approve_manual:{view.candidate_id}").click().run(timeout=10)
        approvals = [item for item in ReviewStore(output / "review_events.jsonl").events
                     if item.get("event") == "review_approval" and item.get("candidate_id") == view.candidate_id]
        self.assertEqual(len(approvals), 1)
        self.assertEqual(approvals[0]["approval_method"], "manual_source_unavailable")
        self.assertEqual(approvals[0]["source_hash"], view.source_hash)
        self.assertTrue(approvals[0]["reviewer_acknowledged"])
        self.assertFalse(app.exception)
        self.assertFalse(app.exception)

    def test_add_instructions_navigation_is_consumed_once_and_keeps_editor_buffer(self):
        output = Path(self.fixture["output"])
        source = Path(self.fixture["source"])
        view = next(item for item in load_catalog(output) if item.candidate.name == "Fried Rice")
        buffer = buffer_for(view)
        buffer["structured_instructions"] = []
        buffer["raw_instructions"] = ""
        save_draft(view, buffer)

        app = AppTest.from_file(str(APP_PATH)).run(timeout=10)
        next(item for item in app.selectbox if item.label == "Show").set_value("All recipes").run(timeout=10)
        next(item for item in app.selectbox if item.label == "Jump to recipe").set_value(view.candidate_id).run(timeout=10)
        app.radio(key=f"instruction_handling:{view.candidate_id}").set_value(
            "Request AI-suggested instructions").run(timeout=10)
        self.assertEqual(app.session_state[f"recipe_sections:{view.candidate_id}"], "Ingredients")
        app.radio(key=f"instruction_handling:{view.candidate_id}").set_value(
            "Add instructions manually").run(timeout=10)
        self.assertEqual(app.session_state[f"recipe_sections:{view.candidate_id}"], "Instructions")
        self.assertNotIn(f"review_pending_section:{view.candidate_id}", app.session_state)
        self.assertTrue(any("Instructions section is open" in item.value for item in app.info))
        self.assertFalse(app.exception)

        next(item for item in app.button if item.key == f"add_step:{view.candidate_id}").click().run(timeout=10)
        step = next(item for item in app.text_area if item.label == "Step 1")
        app.text_area(key=step.key).set_value("Synthetic unsaved preparation step.").run(timeout=10)
        self.assertIn("Synthetic unsaved preparation step.", app.session_state["review_buffers"][view.candidate_id]["structured_instructions"])

        next(item for item in app.selectbox if item.label == "Show").set_value("All recipes").run(timeout=10)
        self.assertEqual(app.session_state[f"recipe_sections:{view.candidate_id}"], "Instructions")
        self.assertIn("Synthetic unsaved preparation step.", app.session_state["review_buffers"][view.candidate_id]["structured_instructions"])
        self.assertFalse(app.exception)

    def test_manual_approval_blocks_an_unsaved_buffer_even_with_a_persisted_preview(self):
        output = Path(self.fixture["output"])
        source = Path(self.fixture["source"])
        view = next(item for item in load_catalog(output) if item.candidate.name == "Fried Rice")
        source.unlink()
        app = AppTest.from_file(str(APP_PATH)).run(timeout=10)
        next(item for item in app.selectbox if item.label == "Show").set_value("All recipes").run(timeout=10)
        next(item for item in app.selectbox if item.label == "Jump to recipe").set_value(view.candidate_id).run(timeout=10)
        self.assertTrue(any("Saved revision" in item.value for item in app.markdown))

        buffers = dict(app.session_state["review_buffers"])
        buffer = dict(buffers[view.candidate_id])
        buffer.update({"name": "Unsaved manual title", "yield_text": "4 servings",
                       "description": "Unsaved manual description", "recipe_notes": "Unsaved note",
                       "structured_ingredients": ["4 cups synthetic rice", "2 synthetic eggs"],
                       "structured_instructions": ["Unsaved first step", "Unsaved second step"]})
        buffers[view.candidate_id] = buffer
        app.session_state["review_buffers"] = buffers
        app.run(timeout=10)

        self.assertTrue(any("unsaved changes" in item.value.casefold() for item in app.warning))
        approve = next(item for item in app.button if item.key == f"approve_manual:{view.candidate_id}")
        self.assertTrue(approve.disabled)
        approvals = [item for item in ReviewStore(output / "review_events.jsonl").events
                     if item.get("event") == "review_approval" and item.get("candidate_id") == view.candidate_id]
        self.assertEqual(approvals, [])
        self.assertFalse(app.exception)

    def test_manual_approval_acknowledgment_resets_when_saved_revision_changes(self):
        output = Path(self.fixture["output"])
        source = Path(self.fixture["source"])
        view = next(item for item in load_catalog(output) if item.candidate.name == "Fried Rice")
        source.unlink()
        app = AppTest.from_file(str(APP_PATH)).run(timeout=10)
        next(item for item in app.selectbox if item.label == "Show").set_value("All recipes").run(timeout=10)
        next(item for item in app.selectbox if item.label == "Jump to recipe").set_value(view.candidate_id).run(timeout=10)
        ack = next(item for item in app.checkbox if item.label.startswith("I have reviewed the recipe"))
        app.checkbox(key=ack.key).check().run(timeout=10)
        self.assertTrue(next(item for item in app.checkbox
                             if item.label.startswith("I have reviewed the recipe")).value)

        revised = view.candidate.model_copy(update={"description": "A different saved revision."})
        ReviewStore(output / "review_events.jsonl").save_revision(
            candidate_id=view.candidate_id, source_hash=view.source_hash,
            extraction_id=view.extraction_id, expected_parent=view.revision,
            operation_id="test-manual-approval-ack-revision-change",
            recipe=revised.model_dump(mode="json"), target_bindings=list(view.target_bindings),
        )
        app.run(timeout=10)
        reset_ack = next(item for item in app.checkbox
                         if item.label.startswith("I have reviewed the recipe"))
        self.assertFalse(reset_ack.value)
        self.assertTrue(next(item for item in app.button
                             if item.key == f"approve_manual:{view.candidate_id}").disabled)
        self.assertFalse(app.exception)


if __name__ == "__main__":
    unittest.main()
