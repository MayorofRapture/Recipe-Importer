from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from recipe_quality import ExtractionBatch, RecipeCandidate
from reconciliation_service import ReconciliationError, RetryService
from review_store import ReviewStore
from tests.ui_fixture_factory import write_ui_fixture
from ui_data import load_catalog

APP_PATH = Path(__file__).resolve().parents[1] / "streamlit_app.py"


class StreamlitReconciliationTests(unittest.TestCase):
    def _attempt(self, root: Path, replacement: RecipeCandidate):
        fixture = write_ui_fixture(root)
        output = Path(fixture["output"])
        target = next(view for view in load_catalog(output)
                      if view.candidate.name == "Fried Rice")
        service = RetryService(
            output,
            extractor=lambda *_: ExtractionBatch(recipes=[replacement]),
        )
        attempt = service.start_attempt(str(fixture["digest"]), mode="local_ocr")
        return fixture, target, attempt

    def _app(self, fixture: dict, attempt_id: str):
        environment = patch.dict(os.environ, {
            "RECIPE_IMPORTER_OUTPUT_DIR": str(fixture["output"]),
            "RECIPE_IMPORTER_SOURCE_DIR": str(Path(fixture["source"]).parent),
        }, clear=False)
        environment.start()
        self.addCleanup(environment.stop)
        app = AppTest.from_file(str(APP_PATH)).run(timeout=15)
        app.session_state["review_comparison_attempt_id"] = attempt_id
        app.run(timeout=15)
        return app

    def test_cancel_comparison_returns_to_draft_without_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture, _target, attempt = self._attempt(
                Path(tmp), RecipeCandidate(name="Fried Rice Revised",
                                           ingredients=["2 cups rice"],
                                           instructions=["Steam the rice."]),
            )
            events_path = Path(fixture["output"]) / "review_events.jsonl"
            before = events_path.read_text(encoding="utf-8")
            app = self._app(fixture, attempt["attempt_id"])
            self.assertFalse(app.exception)
            next(button for button in app.button
                 if button.label == "Return to current draft").click().run(timeout=15)
            after = events_path.read_text(encoding="utf-8")

        self.assertFalse(app.exception)
        self.assertEqual(before, after)
        self.assertTrue(any(item.label == "Recipe title" for item in app.text_input))

    def test_explicit_mapping_updates_only_selected_sections_and_requires_reapproval(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture, target, attempt = self._attempt(
                Path(tmp), RecipeCandidate(name="Fried Rice Revised",
                                           ingredients=["4 cups rice"],
                                           instructions=["Steam, then fold in egg."]),
            )
            reviews = ReviewStore(Path(fixture["output"]) / "review_events.jsonl")
            reviews.save_revision(
                candidate_id=target.candidate_id, source_hash=target.source_hash,
                extraction_id=target.extraction_id, expected_parent=0,
                operation_id="r5-prior-human-edit",
                recipe={**target.candidate.model_dump(mode="json"),
                        "ingredients": ["3 cups corrected rice", "1 egg"]},
                target_bindings=[],
            )
            app = self._app(fixture, attempt["attempt_id"])
            self.assertFalse(app.exception)
            selector = next(item for item in app.selectbox
                            if item.label == "Existing recipe to update")
            selector.select(next(value for value in selector.options if "Fried Rice" in value)).run(timeout=15)
            section_picker = next(item for item in app.multiselect
                                  if item.label == "Sections to use from the new extraction")
            section_picker.set_value(["instructions"]).run(timeout=15)
            next(item for item in app.text_input if item.label == "Why this mapping is correct").set_value(
                "The scan shows the revised instruction sequence.").run(timeout=15)
            next(button for button in app.button
                 if button.label == "Apply selected sections").click().run(timeout=15)
            projected = ReviewStore(Path(fixture["output"]) / "review_events.jsonl").project(
                target.candidate_id, target.source_hash, target.extraction_id, target.candidate,
            )

        self.assertFalse(app.exception)
        self.assertEqual(projected.recipe["ingredients"], ["3 cups corrected rice", "1 egg"])
        self.assertEqual(projected.recipe["instructions"], ["Steam, then fold in egg."])
        self.assertFalse(projected.import_ready)
        self.assertTrue(any("Draft saved" in item.value or "updated" in item.value.casefold()
                            for item in app.success))

    def test_changed_source_mapping_failure_keeps_comparison_and_selected_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture, target, attempt = self._attempt(
                Path(tmp), RecipeCandidate(name="Fried Rice Revised",
                                           ingredients=["4 cups rice"],
                                           instructions=["Steam the rice."]),
            )
            reviews = ReviewStore(Path(fixture["output"]) / "review_events.jsonl")
            reviews.save_revision(
                candidate_id=target.candidate_id, source_hash=target.source_hash,
                extraction_id=target.extraction_id, expected_parent=0,
                operation_id="r5-stale-test-original", recipe=target.candidate.model_dump(mode="json"),
                target_bindings=[],
            )
            app = self._app(fixture, attempt["attempt_id"])
            selector = next(item for item in app.selectbox
                            if item.label == "Existing recipe to update")
            selector.select(next(value for value in selector.options if "Fried Rice" in value)).run(timeout=15)
            next(item for item in app.multiselect
                 if item.label == "Sections to use from the new extraction").set_value(["ingredients"]).run(timeout=15)
            next(item for item in app.text_input if item.label == "Why this mapping is correct").set_value(
                "The source quantity changed.").run(timeout=15)
            original_compare = RetryService.compare
            def changed_source_comparison(service, attempt_id):
                result = original_compare(service, attempt_id)
                result["source_changed"] = True
                return result
            with patch("reconciliation_service.RetryService.compare", changed_source_comparison), \
                    patch("reconciliation_service.RetryService.apply_mapping",
                          side_effect=ReconciliationError("The source changed; relink and verify it before mapping.")):
                next(button for button in app.button
                     if button.label == "Apply selected sections").click().run(timeout=15)

        self.assertFalse(app.exception)
        self.assertTrue(any("remains available" in item.value.casefold()
                            or "source changed" in item.value.casefold()
                            for item in app.error))
        self.assertTrue(any("source bytes differ" in item.value.casefold()
                            for item in app.warning))
        selector_after = next(item for item in app.selectbox
                              if item.label == "Existing recipe to update")
        self.assertIn("Fried Rice", selector_after.value)
        self.assertTrue(any(item.label == "Return to current draft" for item in app.button))

    def test_declared_new_candidate_enters_review_without_import_or_sibling_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture, _target, attempt = self._attempt(
                Path(tmp), RecipeCandidate(name="New family tart",
                                           ingredients=["2 cups berries"],
                                           instructions=["Bake until set."]),
            )
            # A separate declaration cannot bypass a protected sibling from the
            # same source. Keep this positive new-candidate case unprotected;
            # the authority-level hold is covered by its dedicated regression.
            (Path(fixture["output"]) / "import_state.jsonl").unlink()
            sibling_before = next(view for view in load_catalog(Path(fixture["output"]))
                                  if view.candidate.name == "Fried Rice")
            app = self._app(fixture, attempt["attempt_id"])
            selector = next(item for item in app.selectbox
                            if item.label == "Existing recipe to update")
            selector.select("Declare a new recipe").run(timeout=15)
            next(item for item in app.multiselect
                 if item.label == "Sections to carry into a separately reviewed recipe").set_value(
                     ["name", "ingredients", "instructions"]).run(timeout=15)
            next(item for item in app.text_input if item.label == "Why this mapping is correct").set_value(
                "This recipe is separate from the existing candidates in the scan.").run(timeout=15)
            next(button for button in app.button
                 if button.label == "Record as a new recipe").click().run(timeout=15)
            projected = load_catalog(Path(fixture["output"]))
            new_view = next(view for view in projected if view.candidate.name == "New family tart")
            sibling_after = next(view for view in projected
                                 if view.candidate_id == sibling_before.candidate_id)

        self.assertFalse(app.exception)
        self.assertEqual(new_view.revision, 0)
        self.assertFalse(new_view.import_ready)
        self.assertEqual(sibling_after.status, sibling_before.status)
        self.assertTrue(any("separately" in item.value for item in app.success))


if __name__ == "__main__":
    unittest.main()
