"""Focused Review UI regressions for section tabs and inline proposals."""
from __future__ import annotations

import json
import os
import shutil
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from tests.ui_fixture_factory import write_ui_fixture
from ui_data import load_catalog
from recipe_quality import Suggestion
from app_pages.common import save_draft
from review_store import ReviewConflict, ReviewStore


APP_PATH = Path(__file__).resolve().parents[1] / "streamlit_app.py"
TEST_TMP = Path(__file__).resolve().parents[1] / ".test-tmp"
TEST_TMP.mkdir(exist_ok=True)


class ReviewInlineTabTests(unittest.TestCase):
    def _temp_dir(self) -> Path:
        root = TEST_TMP / str(uuid.uuid4())
        root.mkdir()
        self.addCleanup(shutil.rmtree, root)
        return root

    def _launch(self, tmp: str):
        fixture = write_ui_fixture(Path(tmp))
        env = patch.dict(os.environ, {
            "RECIPE_IMPORTER_OUTPUT_DIR": str(fixture["output"]),
            "RECIPE_IMPORTER_SOURCE_DIR": str(Path(tmp) / "sources"),
        }, clear=False)
        env.start()
        self.addCleanup(env.stop)
        return fixture, AppTest.from_file(str(APP_PATH)).run(timeout=10)

    def _instruction_fixture(self, tmp: Path, field_path: str = "instructions[1]"):
        fixture = write_ui_fixture(tmp)
        cache_path = Path(fixture["output"]) / "extracted" / f"{fixture['digest']}.json"
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
        target = next(item for item in cache["batch"]["recipes"]
                      if item["name"] == "Cranberry & Caramel Date Bars")
        target["instructions"] = ["Stir the dates.", "Bake until set."]
        target["suggestions"] = [Suggestion(
            suggestion_id="instruction-proposal", field_path=field_path,
            proposed_value="Bake for 20 minutes.", rationale="A plausible completion.",
            uncertainty="The duration is absent from the scan.",
        ).model_dump(mode="json")]
        cache_path.write_text(json.dumps(cache), encoding="utf-8")
        (Path(fixture["output"]) / "review_events.jsonl").write_text("", encoding="utf-8")
        return fixture

    def test_section_tabs_are_direct_and_preserve_unsaved_edits_when_switched(self):
        with self.subTest("isolated fixture"):
            tmp = self._temp_dir()
            fixture, app = self._launch(str(tmp))
            self.assertFalse(app.exception)
            candidate_id = app.session_state["review_selection"]
            buffer = app.session_state["review_buffers"][candidate_id]
            ingredient = next(item for item in app.text_input if item.label == "Ingredient 1")
            ingredient.set_value("2 cups chilled rice")
            tab_key = f"recipe_sections:{candidate_id}"
            app.session_state[tab_key] = "Instructions"
            app.run(timeout=10)
            self.assertFalse(app.exception)
            app.session_state[tab_key] = "Ingredients"
            app.run(timeout=10)

        self.assertEqual(buffer["structured_ingredients"][0], "2 cups chilled rice")
        self.assertTrue(any(item.label == "Instructions" for item in app.get("tab")))

    def test_optional_metadata_stays_local_until_explicit_save(self):
        tmp = self._temp_dir()
        with self.subTest("isolated fixture"):
            fixture = write_ui_fixture(tmp)
            env = patch.dict(os.environ, {
                "RECIPE_IMPORTER_OUTPUT_DIR": str(fixture["output"]),
                "RECIPE_IMPORTER_SOURCE_DIR": str(tmp / "sources"),
                "RECIPE_IMPORTER_UI_DIAGNOSTICS": "1",
            }, clear=False)
            env.start()
            self.addCleanup(env.stop)
            app = AppTest.from_file(str(APP_PATH)).run(timeout=10)
            candidate_id = app.session_state["review_selection"]
            before_revision = next(item for item in load_catalog(Path(fixture["output"]))
                                   if item.candidate_id == candidate_id).revision

            app.session_state["review_show_optional_details"] = True
            app.run(timeout=10)
            before_writes = app.session_state["_ui_diagnostics"]["durable_writes"]
            next(item for item in app.text_input if item.label == "Recipe title").set_value("Rice for Two").run(timeout=10)
            next(item for item in app.text_input if item.label == "Yield").set_value("2 servings").run(timeout=10)

            # AppTest does not model browser fragment isolation; this contract checks that edits stay local.
            self.assertEqual(app.session_state["_ui_diagnostics"]["durable_writes"], before_writes)
            self.assertEqual(app.session_state["review_buffers"][candidate_id]["name"], "Rice for Two")
            self.assertEqual(app.session_state["review_buffers"][candidate_id]["yield_text"], "2 servings")
            after = next(item for item in load_catalog(Path(fixture["output"]))
                         if item.candidate_id == candidate_id)
            self.assertEqual(after.revision, before_revision)

    def test_queue_advance_reuses_the_catalog_loaded_for_that_page_run(self):
        tmp = self._temp_dir()
        with self.subTest("isolated fixture"):
            fixture = write_ui_fixture(tmp)
            env = patch.dict(os.environ, {
                "RECIPE_IMPORTER_OUTPUT_DIR": str(fixture["output"]),
                "RECIPE_IMPORTER_SOURCE_DIR": str(tmp / "sources"),
            }, clear=False)
            env.start()
            self.addCleanup(env.stop)
            with patch("app_pages.common.load_catalog", wraps=load_catalog) as catalog_loader:
                app = AppTest.from_file(str(APP_PATH)).run(timeout=10)
                candidate_id = app.session_state["review_selection"]
                initial = next(item for item in load_catalog(Path(fixture["output"]), Path(tmp / "sources"))
                               if item.candidate_id == candidate_id)
                next(item for item in app.text_input if item.label == "Ingredient 1").set_value(
                    "1 cup saved before skip").run(timeout=10)
                before = catalog_loader.call_count
                next(item for item in app.button if item.label == "Skip for now").click().run(timeout=10)

                self.assertEqual(catalog_loader.call_count - before, 1)
                self.assertNotEqual(app.session_state["review_selection"], candidate_id)
                saved = next(item for item in load_catalog(Path(fixture["output"]), Path(tmp / "sources"))
                             if item.candidate_id == candidate_id)
                self.assertEqual(saved.revision, initial.revision + 1)
                self.assertEqual(saved.candidate.ingredients[0], "1 cup saved before skip")

    def test_save_after_fragment_revision_does_not_append_duplicate_on_stale_view(self):
        tmp = self._temp_dir()
        fixture = write_ui_fixture(tmp)
        env = patch.dict(os.environ, {
            "RECIPE_IMPORTER_OUTPUT_DIR": str(fixture["output"]),
            "RECIPE_IMPORTER_SOURCE_DIR": str(tmp / "sources"),
        }, clear=False)
        env.start()
        self.addCleanup(env.stop)
        app = AppTest.from_file(str(APP_PATH)).run(timeout=10)
        candidate_id = app.session_state["review_selection"]
        view = next(item for item in load_catalog(Path(fixture["output"]), Path(tmp / "sources"))
                    if item.candidate_id == candidate_id)
        buffer = app.session_state["review_buffers"][candidate_id]

        buffer["name"] = "First saved title"
        first, _ = save_draft(view, buffer)
        buffer["name"] = "Second saved title"
        second, _ = save_draft(view, buffer)
        unchanged, _ = save_draft(view, buffer)

        self.assertEqual(first.revision, view.revision + 1)
        self.assertEqual(second.revision, first.revision + 1)
        self.assertEqual(unchanged.revision, second.revision)
        latest = next(item for item in load_catalog(Path(fixture["output"]), Path(tmp / "sources"))
                      if item.candidate_id == candidate_id)
        self.assertEqual(latest.revision, second.revision)
        self.assertEqual(latest.candidate.name, "Second saved title")

        external = latest.candidate.model_dump(mode="json")
        external["name"] = "External concurrent title"
        ReviewStore(Path(fixture["output"]) / "review_events.jsonl").save_revision(
            candidate_id=candidate_id, source_hash=latest.source_hash,
            extraction_id=latest.extraction_id, expected_parent=second.revision,
            operation_id="external-concurrent-save", recipe=external,
            target_bindings=list(latest.target_bindings),
        )
        with self.assertRaises(ReviewConflict):
            save_draft(view, buffer)
        self.assertEqual(buffer["name"], "Second saved title")

    def test_unsaved_instruction_edit_survives_tab_switch_and_bulk_roundtrip(self):
        with self.subTest("isolated fixture"):
            tmp = self._temp_dir()
            fixture, app = self._launch(str(tmp))
            candidate_id = app.session_state["review_selection"]
            tab_key = f"recipe_sections:{candidate_id}"
            app.session_state[tab_key] = "Instructions"
            app.run(timeout=10)
            next(item for item in app.text_area if item.label == "Step 1").set_value(
                "Cook rice over low heat.").run(timeout=10)
            app.session_state[tab_key] = "Ingredients"
            app.run(timeout=10)
            self.assertEqual(
                app.session_state["review_buffers"][candidate_id]["structured_instructions"][0],
                "Cook rice over low heat.",
            )
            next(button for button in app.button if button.label == "Bulk edit ingredients").click().run(timeout=10)
            next(button for button in app.button if button.label == "Cancel bulk edit").click().run(timeout=10)
            app.session_state[tab_key] = "Instructions"
            app.run(timeout=10)

        self.assertFalse(app.exception)
        self.assertEqual(next(item for item in app.text_area if item.label == "Step 1").value,
                         "Cook rice over low heat.")

    def test_cancel_bulk_edit_discards_only_bulk_text_and_keeps_other_row_edit(self):
        with self.subTest("isolated fixture"):
            tmp = self._temp_dir()
            fixture, app = self._launch(str(tmp))
            app = next(item for item in app.text_input if item.label == "Ingredient 1").set_value(
                "2 cups chilled rice").run(timeout=10)
            candidate_id = app.session_state["review_selection"]
            next(button for button in app.button if button.label == "Bulk edit ingredients").click().run(timeout=10)
            self.assertEqual(next(item for item in app.text_area if item.label == "Bulk ingredients").value,
                             "2 cups chilled rice\n1 egg")
            bulk = next(item for item in app.text_area if item.label == "Bulk ingredients")
            bulk.set_value("3 cups rice\n1 egg").run(timeout=10)
            next(button for button in app.button if button.label == "Cancel bulk edit").click().run(timeout=10)
            self.assertFalse(any(item.label == "Bulk ingredients" for item in app.text_area))
            buffer = app.session_state["review_buffers"][candidate_id]
            next(button for button in app.button if button.label == "Bulk edit ingredients").click().run(timeout=10)
            reopened = next(item for item in app.text_area if item.label == "Bulk ingredients")

        self.assertFalse(app.exception)
        self.assertEqual(buffer["structured_ingredients"][0], "2 cups chilled rice")
        self.assertEqual(buffer["raw_ingredients"], "2 cups chilled rice\n1 egg")
        self.assertEqual(reopened.value, "2 cups chilled rice\n1 egg")

    def test_cancel_bulk_editor_collapses_with_source_notes_open(self):
        with self.subTest("isolated fixture"):
            tmp = self._temp_dir()
            fixture = self._instruction_fixture(tmp)
            env = patch.dict(os.environ, {
                "RECIPE_IMPORTER_OUTPUT_DIR": str(fixture["output"]),
                "RECIPE_IMPORTER_SOURCE_DIR": str(tmp / "sources"),
            }, clear=False)
            env.start()
            self.addCleanup(env.stop)
            app = AppTest.from_file(str(APP_PATH)).run(timeout=10)
            app = next(item for item in app.selectbox if item.label == "Jump to recipe").set_value(
                "Cranberry & Caramel Date Bars").run(timeout=10)
            candidate_id = app.session_state["review_selection"]
            app.session_state[f"recipe_sections:{candidate_id}"] = "Ingredients"
            app.run(timeout=10)
            next(button for button in app.button if button.label == "Bulk edit ingredients").click().run(timeout=10)
            next(item for item in app.text_area if item.label == "Bulk ingredients").set_value(
                "1 cup dates\n2 cups oats").run(timeout=10)
            next(button for button in app.button if button.label == "Cancel bulk edit").click().run(timeout=10)

        self.assertFalse(app.exception)
        self.assertTrue(any("Issues and source notes" in item.label for item in app.expander))
        self.assertFalse(any(item.label == "Bulk ingredients" for item in app.text_area))

    def test_rejecting_suggestion_preserves_critical_finding(self):
        with self.subTest("isolated fixture"):
            tmp = self._temp_dir()
            fixture, app = self._launch(str(tmp))
            app = next(item for item in app.selectbox if item.label == "Jump to recipe").set_value(
                "Cranberry & Caramel Date Bars").run(timeout=10)
            reject = next(button for button in app.button if button.label == "Reject")
            reject.click().run(timeout=10)
            view = next(item for item in load_catalog(Path(fixture["output"]))
                        if item.candidate.name == "Cranberry & Caramel Date Bars")

        self.assertFalse(app.exception)
        self.assertTrue(any(item.severity == "critical" for item in view.candidate.missing_information))
        rejected = next(item for item in view.candidate.suggestions
                        if item.suggestion_id == "cranberry-quantity")
        self.assertEqual(rejected.review_status, "rejected")

    def test_instruction_suggestion_applies_to_its_immutable_step_and_keeps_proposal(self):
        with self.subTest("isolated fixture"):
            tmp = self._temp_dir()
            fixture = self._instruction_fixture(tmp)
            env = patch.dict(os.environ, {
                "RECIPE_IMPORTER_OUTPUT_DIR": str(fixture["output"]),
                "RECIPE_IMPORTER_SOURCE_DIR": str(Path(tmp) / "sources"),
            }, clear=False)
            env.start()
            self.addCleanup(env.stop)
            app = AppTest.from_file(str(APP_PATH)).run(timeout=10)
            app = next(item for item in app.selectbox if item.label == "Jump to recipe").set_value(
                "Cranberry & Caramel Date Bars").run(timeout=10)
            candidate_id = app.session_state["review_selection"]
            app.session_state[f"recipe_sections:{candidate_id}"] = "Ingredients"
            app.run(timeout=10)
            next(item for item in app.text_input if item.label == "Ingredient 1").set_value(
                "2 cups dates, chopped")
            app.session_state[f"recipe_sections:{candidate_id}"] = "Instructions"
            app.run(timeout=10)
            next(button for button in app.button if button.label == "Edit suggestion").click().run(timeout=10)
            next(item for item in app.text_area if item.label == "Edit suggestion").set_value(
                "Bake for 25 minutes.").run(timeout=10)
            app.session_state[f"recipe_sections:{candidate_id}"] = "Ingredients"
            app.run(timeout=10)
            app.session_state[f"recipe_sections:{candidate_id}"] = "Instructions"
            app.run(timeout=10)
            self.assertEqual(next(item for item in app.text_area if item.label == "Edit suggestion").value,
                             "Bake for 25 minutes.")
            next(button for button in app.button if button.label == "Use edited version").click().run(timeout=10)
            visible_step = next(item for item in app.text_area if item.label == "Step 2")
            app.session_state[f"recipe_sections:{candidate_id}"] = "Ingredients"
            app.run(timeout=10)
            app.session_state[f"recipe_sections:{candidate_id}"] = "Instructions"
            app.run(timeout=10)
            visible_step_after_switch = next(item for item in app.text_area if item.label == "Step 2")
            next(button for button in app.button if button.label == "Save draft").click().run(timeout=10)
            visible_step_after_save = next(item for item in app.text_area if item.label == "Step 2")
            events = [json.loads(line) for line in
                      (Path(fixture["output"]) / "review_events.jsonl").read_text(encoding="utf-8").splitlines()]
            view = next(item for item in load_catalog(Path(fixture["output"]))
                        if item.candidate.name == "Cranberry & Caramel Date Bars")

        revision = next(item for item in events if item.get("event") == "review_revision"
                        and item.get("recipe", {}).get("name") == "Cranberry & Caramel Date Bars")
        self.assertFalse(app.exception)
        self.assertEqual(visible_step.value, "Bake for 25 minutes.")
        self.assertEqual(visible_step_after_switch.value, "Bake for 25 minutes.")
        self.assertEqual(visible_step_after_save.value, "Bake for 25 minutes.")
        self.assertEqual(sum(item.get("event") == "review_revision" for item in events), 1)
        self.assertEqual(revision["recipe"]["instructions"][1], "Bake for 25 minutes.")
        self.assertEqual(revision["recipe"]["ingredients"][0], "2 cups dates, chopped")
        self.assertEqual(revision["recipe"]["suggestions"][0]["proposed_value"], "Bake for 20 minutes.")
        self.assertEqual(view.candidate.suggestions[0].review_status, "edited")

    def test_whole_instructions_proposal_applies_as_a_section_value(self):
        with self.subTest("isolated fixture"):
            tmp = self._temp_dir()
            fixture = self._instruction_fixture(tmp, "instructions")
            env = patch.dict(os.environ, {
                "RECIPE_IMPORTER_OUTPUT_DIR": str(fixture["output"]),
                "RECIPE_IMPORTER_SOURCE_DIR": str(tmp / "sources"),
            }, clear=False)
            env.start()
            self.addCleanup(env.stop)
            app = AppTest.from_file(str(APP_PATH)).run(timeout=10)
            app = next(item for item in app.selectbox if item.label == "Jump to recipe").set_value(
                "Cranberry & Caramel Date Bars").run(timeout=10)
            candidate_id = app.session_state["review_selection"]
            app.session_state[f"recipe_sections:{candidate_id}"] = "Instructions"
            app.run(timeout=10)
            next(button for button in app.button if button.label == "Use suggestion").click().run(timeout=10)
            events = [json.loads(line) for line in
                      (Path(fixture["output"]) / "review_events.jsonl").read_text(encoding="utf-8").splitlines()]

        revision = next(item for item in events if item.get("event") == "review_revision"
                        and item.get("recipe", {}).get("name") == "Cranberry & Caramel Date Bars")
        self.assertFalse(app.exception)
        self.assertEqual(revision["recipe"]["instructions"], ["Bake for 20 minutes."])

    def test_unindexed_instruction_proposal_is_visible_but_never_bound_by_position(self):
        with self.subTest("isolated fixture"):
            tmp = self._temp_dir()
            fixture = self._instruction_fixture(tmp, "instructions[9]")
            env = patch.dict(os.environ, {
                "RECIPE_IMPORTER_OUTPUT_DIR": str(fixture["output"]),
                "RECIPE_IMPORTER_SOURCE_DIR": str(tmp / "sources"),
            }, clear=False)
            env.start()
            self.addCleanup(env.stop)
            app = AppTest.from_file(str(APP_PATH)).run(timeout=10)
            app = next(item for item in app.selectbox if item.label == "Jump to recipe").set_value(
                "Cranberry & Caramel Date Bars").run(timeout=10)
            candidate_id = app.session_state["review_selection"]
            app.session_state[f"recipe_sections:{candidate_id}"] = "Instructions"
            app.run(timeout=10)

        self.assertFalse(app.exception)
        self.assertTrue(any("No exact original row is available" in item.value for item in app.caption))
        apply_button = next(button for button in app.button if button.label == "Use suggestion")
        self.assertTrue(apply_button.disabled)


if __name__ == "__main__":
    unittest.main()
