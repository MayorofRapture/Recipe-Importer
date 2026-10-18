"""Focused Review UI regressions for section tabs and inline proposals."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from tests.ui_fixture_factory import write_ui_fixture
from ui_data import load_catalog, verify_selected_source
from recipe_quality import Suggestion
from app_pages.common import buffer_for, save_draft
from review_store import ReviewConflict, ReviewStore


APP_PATH = Path(__file__).resolve().parents[1] / "streamlit_app.py"
class ReviewInlineTabTests(unittest.TestCase):
    def _temp_dir(self) -> Path:
        temporary_root = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_root.cleanup)
        return Path(temporary_root.name)

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
            reject = next(button for button in app.button if button.label == "Reject; keep issue open")
            reject.click().run(timeout=10)
            view = next(item for item in load_catalog(Path(fixture["output"]))
                        if item.candidate.name == "Cranberry & Caramel Date Bars")

        self.assertFalse(app.exception)
        self.assertTrue(any(item.severity == "critical" for item in view.candidate.missing_information))
        rejected = next(item for item in view.candidate.suggestions
                        if item.suggestion_id == "cranberry-quantity")
        self.assertEqual(rejected.review_status, "rejected")

    def test_approval_blocker_review_action_opens_issues(self):
        tmp = self._temp_dir()
        fixture = write_ui_fixture(tmp)
        env = patch.dict(os.environ, {
            "RECIPE_IMPORTER_OUTPUT_DIR": str(fixture["output"]),
            "RECIPE_IMPORTER_SOURCE_DIR": str(tmp / "sources"),
        }, clear=False)
        env.start()
        self.addCleanup(env.stop)
        app = AppTest.from_file(str(APP_PATH)).run(timeout=10)
        app = next(item for item in app.selectbox if item.label == "Jump to recipe").set_value(
            "Cranberry & Caramel Date Bars").run(timeout=10)
        self.assertTrue(any("critical source issue unresolved" in item.value for item in app.warning))
        blocker_review = next(item for item in app.button if item.label == "Review")
        blocker_review.click().run(timeout=10)
        self.assertTrue(any("The quantity is unclear in the source" in item.value for item in app.warning))

    def test_stale_suggestion_blocker_opens_instruction_tab_without_widget_state_error(self):
        tmp = self._temp_dir()
        fixture = write_ui_fixture(tmp)
        output = Path(fixture["output"])
        source_root = tmp / "sources"
        env = patch.dict(os.environ, {
            "RECIPE_IMPORTER_OUTPUT_DIR": str(output),
            "RECIPE_IMPORTER_SOURCE_DIR": str(source_root),
        }, clear=False)
        env.start()
        self.addCleanup(env.stop)

        view = next(item for item in load_catalog(output, source_root)
                    if item.candidate.name == "Cranberry & Caramel Date Bars")
        view = verify_selected_source(view)
        save_draft(view, buffer_for(view))
        view = next(item for item in load_catalog(output, source_root)
                    if item.candidate.name == "Cranberry & Caramel Date Bars")
        view = verify_selected_source(view)
        target = next(item for item in view.revision_target_bindings
                      if item.get("field") == "instructions")
        from suggestion_service import SuggestionService
        service = SuggestionService(output, proposer=lambda snapshot, kind: [{
            "field_path": "instructions[0]", "target_id": target["target_id"],
            "proposed_value": "Stir the dates gently.", "classification": "inferred",
            "rationale": "A plausible refinement.", "uncertainty": "The source wording is brief.",
        }])
        request = service.request({
            "candidate_id": view.candidate_id, "source_hash": view.source_hash,
            "extraction_id": view.extraction_id, "revision": view.revision,
            "content_digest": view.content_digest, "candidate": view.candidate,
            "source_verified": True,
            "revision_target_bindings": view.revision_target_bindings,
        }, consent=True, operation_id="stale-tab-jump")
        service.process(request["request_id"])

        app = AppTest.from_file(str(APP_PATH)).run(timeout=10)
        app = next(item for item in app.selectbox if item.label == "Jump to recipe").set_value(
            "Cranberry & Caramel Date Bars").run(timeout=10)
        candidate_id = app.session_state["review_selection"]
        app.session_state[f"recipe_sections:{candidate_id}"] = "Instructions"
        app.run(timeout=10)
        next(item for item in app.text_area if item.label == "Step 1").set_value(
            "Stir the dates carefully.")
        next(item for item in app.button if item.label == "Save draft").click().run(timeout=10)
        app.run(timeout=10)
        refreshed = next(item for item in load_catalog(output, source_root)
                         if item.candidate.name == "Cranberry & Caramel Date Bars")
        dynamic = next(item for item in refreshed.candidate.suggestions
                       if getattr(item, "generated", False))
        self.assertEqual(dynamic.dynamic_status, "stale")
        blocker = next(item for item in app.button
                       if item.label == "Review" and "stale" in item.key)
        blocker.click().run(timeout=10)

        self.assertFalse(app.exception)
        self.assertEqual(app.session_state[f"recipe_sections:{candidate_id}"], "Instructions")
        self.assertTrue(any(item.label == "Discard stale suggestion" for item in app.button))

    def test_second_generated_target_can_be_explicitly_rebased_after_first_is_applied(self):
        tmp = self._temp_dir()
        fixture = write_ui_fixture(tmp)
        source_root = tmp / "sources"
        env = patch.dict(os.environ, {
            "RECIPE_IMPORTER_OUTPUT_DIR": str(fixture["output"]),
            "RECIPE_IMPORTER_SOURCE_DIR": str(source_root),
        }, clear=False)
        env.start()
        self.addCleanup(env.stop)

        view = next(item for item in load_catalog(fixture["output"], source_root)
                    if item.candidate.name == "Cranberry & Caramel Date Bars")
        view = verify_selected_source(view)
        save_draft(view, buffer_for(view))
        view = next(item for item in load_catalog(fixture["output"], source_root)
                    if item.candidate.name == "Cranberry & Caramel Date Bars")
        view = verify_selected_source(view)
        target = next(item for item in view.revision_target_bindings
                      if item.get("field") == "instructions" and item.get("position") == 0)

        from suggestion_service import SuggestionService
        proposals = [
            {"field_path": "instructions[0]", "target_id": target["target_id"],
             "proposed_value": "Stir the dates gently.", "classification": "inferred",
             "rationale": "A possible clarification.", "uncertainty": "The scan is brief."},
            {"field_path": "instructions[0]", "target_id": target["target_id"],
             "proposed_value": "Stir the dates until evenly coated.", "classification": "inferred",
             "rationale": "An alternative completion.", "uncertainty": "The scan is brief."},
        ]
        service = SuggestionService(fixture["output"], proposer=lambda _snapshot, _kind: proposals)
        request = service.request({
            "candidate_id": view.candidate_id, "source_hash": view.source_hash,
            "extraction_id": view.extraction_id, "revision": view.revision,
            "content_digest": view.content_digest, "candidate": view.candidate,
            "source_verified": True,
            "revision_target_bindings": view.revision_target_bindings,
        }, consent=True, operation_id="two-target-proposals")
        service.process(request["request_id"])

        app = AppTest.from_file(str(APP_PATH)).run(timeout=10)
        app = next(item for item in app.selectbox if item.label == "Jump to recipe").set_value(
            "Cranberry & Caramel Date Bars").run(timeout=10)
        candidate_id = app.session_state["review_selection"]
        app.session_state[f"recipe_sections:{candidate_id}"] = "Instructions"
        app.run(timeout=10)
        next(item for item in app.button if item.label == "Accept inferred suggestion").click().run(timeout=10)

        refreshed = next(item for item in load_catalog(fixture["output"], source_root)
                         if item.candidate_id == candidate_id)
        dynamic = [item for item in refreshed.candidate.suggestions
                   if getattr(item, "generated", False)]
        self.assertEqual(refreshed.revision, view.revision + 1)
        self.assertEqual(sum(getattr(item, "dynamic_status", "") == "stale" for item in dynamic), 1)
        stale = next(item for item in dynamic if getattr(item, "dynamic_status", "") == "stale")
        stale_checkbox = next(item for item in app.checkbox
                              if item.label == "I compared the current recipe wording with this proposal"
                              and stale.suggestion_id in item.key)
        self.assertTrue(any(item.label == "Use with current revision" for item in app.button))
        self.assertTrue(any(stale.proposed_value in str(item.value) for item in app.markdown))

        stale_checkbox.check().run(timeout=10)
        next(item for item in app.button if item.label == "Use with current revision").click().run(timeout=10)

        after = next(item for item in load_catalog(fixture["output"], source_root)
                     if item.candidate_id == candidate_id)
        self.assertEqual(after.revision, view.revision + 2)
        self.assertEqual(after.candidate.instructions[0], stale.proposed_value)
        decision = next(item for item in reversed(ReviewStore(Path(fixture["output"]) / "review_events.jsonl").events)
                        if item.get("event") == "dynamic_suggestion_decision"
                        and item.get("proposal_id") == stale.suggestion_id)
        self.assertEqual(decision.get("revision"), after.revision)
        self.assertEqual(decision.get("rebase_from_revision"), view.revision)
        self.assertEqual(decision.get("rebase_to_revision"), view.revision + 1)
        self.assertEqual(len(after.critical_findings), 1)

    def test_instruction_request_discloses_text_only_and_requires_cloud_consent(self):
        tmp = self._temp_dir()
        fixture = write_ui_fixture(tmp)
        env = patch.dict(os.environ, {
            "RECIPE_IMPORTER_OUTPUT_DIR": str(fixture["output"]),
            "RECIPE_IMPORTER_SOURCE_DIR": str(tmp / "sources"),
            "GEMINI_API_KEY": "",
        }, clear=False)
        env.start()
        self.addCleanup(env.stop)
        app = AppTest.from_file(str(APP_PATH)).run(timeout=10)
        app = next(item for item in app.selectbox if item.label == "Jump to recipe").set_value(
            "Cranberry & Caramel Date Bars").run(timeout=10)
        candidate_id = app.session_state["review_selection"]
        app.session_state[f"recipe_sections:{candidate_id}"] = "Instructions"
        app.run(timeout=10)

        self.assertTrue(any(
            item.label == "I authorize sending this recipe's title, ingredients, and current instructions to Google Gemini."
            for item in app.checkbox
        ))
        self.assertTrue(any("no source image or PDF is sent" in item.value for item in app.caption))
        request_button = next(item for item in app.button if item.label == "Suggest improvements with AI")
        self.assertTrue(request_button.disabled)
        store = ReviewStore(Path(fixture["output"]) / "review_events.jsonl")
        self.assertFalse(any(item.get("event") == "suggestion_request" for item in store.events))

    def test_review_ui_recognizes_project_env_gemini_key_without_displaying_it(self):
        tmp = self._temp_dir()
        fixture = write_ui_fixture(tmp)
        env_file = tmp / ".env"
        env_file.write_text("GEMINI_API_KEY=review-ui-test-key\n", encoding="utf-8")
        previous_key = os.environ.pop("GEMINI_API_KEY", None)

        def restore_key():
            if previous_key is None:
                os.environ.pop("GEMINI_API_KEY", None)
            else:
                os.environ["GEMINI_API_KEY"] = previous_key

        self.addCleanup(restore_key)
        env = patch.dict(os.environ, {
            "RECIPE_IMPORTER_OUTPUT_DIR": str(fixture["output"]),
            "RECIPE_IMPORTER_SOURCE_DIR": str(tmp / "sources"),
        }, clear=False)
        env.start()
        self.addCleanup(env.stop)

        with patch("gemini_config.PROJECT_ENV_FILE", env_file):
            app = AppTest.from_file(str(APP_PATH)).run(timeout=10)
            app = next(item for item in app.selectbox if item.label == "Jump to recipe").set_value(
                "Cranberry & Caramel Date Bars").run(timeout=10)
            candidate_id = app.session_state["review_selection"]
            app.session_state[f"recipe_sections:{candidate_id}"] = "Instructions"
            app.run(timeout=10)
            request_button = next(item for item in app.button
                                  if item.label == "Suggest improvements with AI")
            self.assertTrue(request_button.disabled)
            consent = next(item for item in app.checkbox
                           if item.label == "I authorize sending this recipe's title, ingredients, and current instructions to Google Gemini.")
            consent.check().run(timeout=10)
            request_button = next(item for item in app.button
                                  if item.label == "Suggest improvements with AI")
            self.assertFalse(request_button.disabled)

        visible_text = " ".join(item.value for item in [*app.caption, *app.markdown, *app.warning, *app.info])
        self.assertNotIn("review-ui-test-key", visible_text)

    def test_missing_source_and_empty_instructions_keep_request_action_visible(self):
        for instructions in ([], [""]):
            with self.subTest(instructions=instructions):
                tmp = self._temp_dir()
                fixture = write_ui_fixture(tmp)
                source_root = tmp / "sources"
                env = patch.dict(os.environ, {
                    "RECIPE_IMPORTER_OUTPUT_DIR": str(fixture["output"]),
                    "RECIPE_IMPORTER_SOURCE_DIR": str(source_root),
                    "RECIPE_IMPORTER_FIXTURE_MODE": "false",
                    "GEMINI_API_KEY": "test-only-key",
                }, clear=False)
                env.start()
                self.addCleanup(env.stop)

                view = next(item for item in load_catalog(Path(fixture["output"]), source_root)
                            if item.candidate.name == "Cranberry & Caramel Date Bars")
                buffer = buffer_for(view)
                buffer["name"] = "RASPBERRY CREAM CENTER"
                buffer["structured_instructions"] = list(instructions)
                buffer["instruction_ids"] = []
                save_draft(view, buffer)
                Path(fixture["source"]).unlink()

                app = AppTest.from_file(str(APP_PATH)).run(timeout=10)
                app = next(item for item in app.selectbox if item.label == "Jump to recipe").set_value(
                    "RASPBERRY CREAM CENTER").run(timeout=10)
                candidate_id = app.session_state["review_selection"]
                app.session_state[f"recipe_sections:{candidate_id}"] = "Instructions"
                app.run(timeout=10)

                self.assertTrue(any(
                    "Original scan unavailable. Gemini can suggest instructions using the recipe text" in item.value
                    for item in app.warning
                ))
                self.assertTrue(next(item for item in app.button
                                     if item.label == "Approve & next").disabled)
                button = next(item for item in app.button
                              if item.label == "Suggest missing instructions with AI")
                self.assertTrue(button.disabled)
                consent = next(item for item in app.checkbox
                               if item.label == "I authorize sending this recipe's title, ingredients, and current instructions to Google Gemini.")
                consent.check().run(timeout=10)
                button = next(item for item in app.button
                              if item.label == "Suggest missing instructions with AI")
                self.assertFalse(button.disabled)

    def test_unavailable_source_text_only_proposal_can_be_accepted_but_not_approved(self):
        tmp = self._temp_dir()
        fixture = write_ui_fixture(tmp)
        output = Path(fixture["output"])
        source_root = tmp / "sources"
        env = patch.dict(os.environ, {
            "RECIPE_IMPORTER_OUTPUT_DIR": str(output),
            "RECIPE_IMPORTER_SOURCE_DIR": str(source_root),
            "RECIPE_IMPORTER_FIXTURE_MODE": "false",
            "GEMINI_API_KEY": "test-only-key",
        }, clear=False)
        env.start()
        self.addCleanup(env.stop)

        view = next(item for item in load_catalog(output, source_root)
                    if item.candidate.name == "Fried Rice")
        buffer = buffer_for(view)
        buffer["name"] = "RASPBERRY CREAM CENTER"
        buffer["structured_instructions"] = []
        buffer["instruction_ids"] = []
        save_draft(view, buffer)
        Path(fixture["source"]).unlink()
        from suggestion_service import SuggestionService
        service = SuggestionService(output, proposer=lambda *_: [{
            "field_path": "instructions", "proposed_value": "Simmer the rice until tender.",
            "classification": "inferred", "rationale": "The ingredient list supports a basic cooking step.",
            "uncertainty": "The scan is unavailable, so timing and method are not verified.",
            "insertion_location": {"field": "instructions", "after": None},
        }])
        submitted = []

        def submit(current_view, *, operation_id):
            request = service.request(current_view, consent=True, operation_id=operation_id)
            service.process(request["request_id"])
            submitted.append(request["request_id"])
            return request

        with patch("app_pages.common.enqueue_suggestion_request", side_effect=submit), patch(
                "app_pages.review.enqueue_suggestion_request", side_effect=submit):
            app = AppTest.from_file(str(APP_PATH)).run(timeout=10)
            app = next(item for item in app.selectbox if item.label == "Jump to recipe").set_value(
                "RASPBERRY CREAM CENTER").run(timeout=10)
            candidate_id = app.session_state["review_selection"]
            app.session_state[f"recipe_sections:{candidate_id}"] = "Instructions"
            app.run(timeout=10)
            next(item for item in app.checkbox
                 if item.label == "I authorize sending this recipe's title, ingredients, and current instructions to Google Gemini.").check().run(timeout=10)
            request_button = next(item for item in app.button
                                  if item.label == "Suggest missing instructions with AI")
            self.assertFalse(request_button.disabled)
            request_button.click().run(timeout=10)
            self.assertFalse(app.exception)
            self.assertEqual(service.status(submitted[0])["status"], "completed")
            self.assertTrue(any("Simmer the rice until tender." in item.value for item in app.markdown))
            next(item for item in app.button if item.label == "Accept inferred suggestion").click().run(timeout=10)

        refreshed = next(item for item in load_catalog(output, source_root)
                         if item.candidate_id == candidate_id)
        self.assertEqual(refreshed.candidate.instructions, ["Simmer the rice until tender."])
        self.assertFalse(refreshed.source_available)
        self.assertTrue(next(item for item in app.button if item.label == "Approve & next").disabled)
        self.assertTrue(any("Original source unavailable" in item.value for item in app.warning))

    def test_fresh_session_discovers_queued_request_and_disables_approval(self):
        tmp = self._temp_dir()
        fixture = write_ui_fixture(tmp)
        source_root = tmp / "sources"
        env = patch.dict(os.environ, {
            "RECIPE_IMPORTER_OUTPUT_DIR": str(fixture["output"]),
            "RECIPE_IMPORTER_SOURCE_DIR": str(source_root),
            "GEMINI_API_KEY": "",
        }, clear=False)
        env.start()
        self.addCleanup(env.stop)
        view = next(item for item in load_catalog(Path(fixture["output"]), source_root)
                    if item.candidate.name == "Fried Rice")
        self.assertTrue(view.source_verified)
        save_draft(view, buffer_for(view))
        view = next(item for item in load_catalog(Path(fixture["output"]), source_root)
                    if item.candidate.name == "Fried Rice")

        # Warm the page's durable-request cache with a negative result before a
        # request is created, then model the local request callback invalidating it.
        app = AppTest.from_file(str(APP_PATH)).run(timeout=10)
        app = next(item for item in app.selectbox if item.label == "Jump to recipe").set_value(
            "Fried Rice").run(timeout=10)
        candidate_id = app.session_state["review_selection"]
        cache_key = f"suggestion_request_snapshot:{candidate_id}"
        self.assertIn(cache_key, app.session_state)
        self.assertIsNone(app.session_state[cache_key]["request"])

        from suggestion_service import SuggestionService
        request = SuggestionService(fixture["output"]).request(
            {"candidate_id": view.candidate_id, "source_hash": view.source_hash,
             "extraction_id": view.extraction_id, "revision": view.revision,
             "content_digest": view.content_digest, "candidate": view.candidate,
             "source_verified": True, "revision_target_bindings": ()},
            consent=True, operation_id="queued-before-new-session")
        self.assertEqual(request["status"], "queued")

        # This is the same invalidation done by the request UI after persistence.
        del app.session_state[cache_key]
        app.run(timeout=10)
        approve = next(item for item in app.button if item.label == "Approve & next")
        self.assertTrue(approve.disabled)
        self.assertTrue(any("AI suggestion request in progress" in item.value
                            for item in app.warning) or any(
                                "AI suggestion request in progress" in item.value for item in app.info))
        self.assertTrue(any(item.label == "Resume queued request" for item in app.button))
        self.assertTrue(any(item.label == "Cancel request" for item in app.button))

        next(item for item in app.button if item.label == "Cancel request").click().run(timeout=10)
        approve = next(item for item in app.button if item.label == "Approve & next")
        self.assertFalse(approve.disabled)

    def test_idle_negative_request_cache_does_not_construct_the_service(self):
        tmp = self._temp_dir()
        fixture = write_ui_fixture(tmp)
        env = patch.dict(os.environ, {
            "RECIPE_IMPORTER_OUTPUT_DIR": str(fixture["output"]),
            "RECIPE_IMPORTER_SOURCE_DIR": str(tmp / "sources"),
        }, clear=False)
        env.start()
        self.addCleanup(env.stop)
        with patch("app_pages.review.suggestion_service") as service_factory, patch(
                "app_pages.review._poll_suggestion_job_status") as poll_fragment:
            app = AppTest.from_file(str(APP_PATH)).run(timeout=10)
            candidate_id = app.session_state["review_selection"]
            self.assertIn(f"suggestion_request_snapshot:{candidate_id}", app.session_state)
            self.assertEqual(service_factory.call_count, 0)
            self.assertEqual(poll_fragment.call_count, 0)
            app.run(timeout=10)
            self.assertEqual(service_factory.call_count, 0)
            self.assertEqual(poll_fragment.call_count, 0)

    def test_fresh_consent_can_request_again_after_completed_empty_result(self):
        tmp = self._temp_dir()
        fixture = write_ui_fixture(tmp)
        source_root = tmp / "sources"
        env = patch.dict(os.environ, {
            "RECIPE_IMPORTER_OUTPUT_DIR": str(fixture["output"]),
            "RECIPE_IMPORTER_SOURCE_DIR": str(source_root),
            "RECIPE_IMPORTER_FIXTURE_MODE": "false",
            "GEMINI_API_KEY": "test-only-key",
        }, clear=False)
        env.start()
        self.addCleanup(env.stop)

        from suggestion_service import SuggestionService
        service = SuggestionService(fixture["output"], proposer=lambda *_: [])
        view = next(item for item in load_catalog(fixture["output"], source_root)
                    if item.candidate.name == "Fried Rice")
        view = verify_selected_source(view)
        save_draft(view, buffer_for(view))
        view = next(item for item in load_catalog(fixture["output"], source_root)
                    if item.candidate.name == "Fried Rice")
        view = verify_selected_source(view)
        completed = service.request(view, consent=True, operation_id="completed-empty-seed")
        service.process(completed["request_id"])
        self.assertEqual(service.status(completed["request_id"])["status"], "completed")

        submitted = []

        def submit(current_view, *, operation_id):
            request = service.request(current_view, consent=True, operation_id=operation_id)
            submitted.append((operation_id, request["request_id"]))
            service.process(request["request_id"])
            return request

        with patch("app_pages.common.enqueue_suggestion_request", side_effect=submit), patch(
                "app_pages.review.enqueue_suggestion_request", side_effect=submit):
            app = AppTest.from_file(str(APP_PATH)).run(timeout=10)
            app = next(item for item in app.selectbox if item.label == "Jump to recipe").set_value(
                "Fried Rice").run(timeout=10)
            candidate_id = app.session_state["review_selection"]
            app.session_state[f"recipe_sections:{candidate_id}"] = "Instructions"
            app.run(timeout=10)
            nonce_key = f"suggestion_consent_nonce:{candidate_id}"
            first_nonce = app.session_state[nonce_key]

            consent = next(item for item in app.checkbox
                           if item.key == f"suggestion_consent:{candidate_id}:{view.revision}:{first_nonce}")
            consent.check().run(timeout=10)
            request_button = next(item for item in app.button
                                  if item.label == "Suggest improvements with AI")
            self.assertFalse(request_button.disabled)
            request_button.click().run(timeout=10)
            second_nonce = app.session_state[nonce_key]
            self.assertNotEqual(first_nonce, second_nonce)
            self.assertEqual(service.status(submitted[0][1])["status"], "completed")

            next_consent = next(item for item in app.checkbox
                                if item.key == f"suggestion_consent:{candidate_id}:{view.revision}:{second_nonce}")
            next_consent.check().run(timeout=10)
            next(item for item in app.button if item.label == "Suggest improvements with AI").click().run(timeout=10)

        self.assertEqual(len(submitted), 2)
        self.assertNotEqual(submitted[0][0], submitted[1][0])
        self.assertNotEqual(submitted[0][1], submitted[1][1])
        self.assertEqual(service.status(submitted[1][1])["status"], "completed")

    def test_using_inferred_proposal_does_not_clear_matching_critical_finding(self):
        tmp = self._temp_dir()
        fixture = write_ui_fixture(tmp)
        env = patch.dict(os.environ, {
            "RECIPE_IMPORTER_OUTPUT_DIR": str(fixture["output"]),
            "RECIPE_IMPORTER_SOURCE_DIR": str(tmp / "sources"),
        }, clear=False)
        env.start()
        self.addCleanup(env.stop)
        app = AppTest.from_file(str(APP_PATH)).run(timeout=10)
        app = next(item for item in app.selectbox if item.label == "Jump to recipe").set_value(
            "Cranberry & Caramel Date Bars").run(timeout=10)
        next(button for button in app.button if button.label == "Accept inferred suggestion").click().run(timeout=10)

        store = ReviewStore(Path(fixture["output"]) / "review_events.jsonl")
        resolutions = [item for item in store.events if item.get("event") == "review_issue_resolution"]
        self.assertTrue(any(item.get("issue_id") == "cranberry-quantity" for item in resolutions))
        self.assertFalse(any(item.get("target_kind") == "finding" for item in resolutions))
        refreshed = next(item for item in load_catalog(Path(fixture["output"]), tmp / "sources")
                         if item.candidate.name == "Cranberry & Caramel Date Bars")
        self.assertEqual(len(refreshed.critical_findings), 1)

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
            next(button for button in app.button if button.label == "Edit and use").click().run(timeout=10)
            next(item for item in app.text_area if item.label == "Edit and use").set_value(
                "Bake for 25 minutes.").run(timeout=10)
            app.session_state[f"recipe_sections:{candidate_id}"] = "Ingredients"
            app.run(timeout=10)
            app.session_state[f"recipe_sections:{candidate_id}"] = "Instructions"
            app.run(timeout=10)
            self.assertEqual(next(item for item in app.text_area if item.label == "Edit and use").value,
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
            next(button for button in app.button if button.label == "Accept inferred suggestion").click().run(timeout=10)
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
        self.assertTrue(any("original target is unavailable" in item.value for item in app.caption))
        self.assertFalse(any(button.label == "Accept inferred suggestion" for button in app.button))


if __name__ == "__main__":
    unittest.main()
