from __future__ import annotations

import os
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from recipe_quality import ExtractionBatch, RecipeCandidate, Suggestion
from reconciliation_service import ReconciliationError, RetryService
from review_store import ReviewStore, _candidate_payload, _canonical, extraction_identity
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

    def test_identity_review_button_opens_comparison_and_confirmation_unblocks_only_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = write_ui_fixture(Path(tmp))
            output = Path(fixture["output"])
            (output / "import_state.jsonl").unlink()
            existing = next(view for view in load_catalog(output)
                            if view.candidate.name == "Fried Rice")
            updated = existing.candidate.model_copy(update={"suggestions": [Suggestion(
                suggestion_id="new-review-proposal", field_path="instructions[0]",
                proposed_value="Fold in the egg gently.", rationale="Possible omitted detail.",
            )]})
            extraction_id = extraction_identity(existing.source_hash, {
                "recipes": [updated.model_dump(mode="json")], "general_warnings": [],
            })
            (output / "extracted" / f"{existing.source_hash}--identity-v2.json").write_text(
                json.dumps({"source_hash": existing.source_hash,
                            "batch": {"recipes": [updated.model_dump(mode="json")]}}),
                encoding="utf-8",
            )
            payload = _candidate_payload(updated)
            event = ReviewStore(output / "review_events.jsonl")._base_event(
                "candidate_registration", "held-identity-v2", existing.source_hash,
                extraction_id, "synthetic-legacy-held-v2",
            )
            event.update({"source": str(fixture["source"]),
                          "fingerprint": hashlib.sha256(_canonical(payload).encode()).hexdigest(),
                          "identity_status": "needs_reconciliation", "legacy_id": "",
                          "title_legacy_id": "", "legacy_aliases": [], "candidate": payload,
                          "target_bindings": []})
            with (output / "review_events.jsonl").open("a", encoding="utf-8") as ledger:
                ledger.write(json.dumps(event) + "\n")
            environment = patch.dict(os.environ, {
                "RECIPE_IMPORTER_OUTPUT_DIR": str(output),
                "RECIPE_IMPORTER_SOURCE_DIR": str(Path(fixture["source"]).parent),
            }, clear=False)
            environment.start()
            self.addCleanup(environment.stop)
            app = AppTest.from_file(str(APP_PATH)).run(timeout=15)
            picker = next((item for item in app.selectbox if item.label == "Jump to recipe"), None)
            self.assertIsNotNone(picker, [item.label for item in app.selectbox])
            picker.select("held-identity-v2").run(timeout=15)
            next(button for button in app.button if button.label == "Resolve identity").click().run(timeout=15)
            self.assertTrue(any("recipe content matches an earlier extraction" in item.value.casefold()
                                for item in app.info))
            self.assertTrue(any(button.label == "Confirm identity match" for button in app.button))
            next(button for button in app.button if button.label == "Confirm identity match").click().run(timeout=15)
            view = next(item for item in load_catalog(output)
                        if item.candidate_id == "held-identity-v2")
            remaining_blockers = [item.value for item in app.warning]
            events = [json.loads(line) for line in
                      (output / "review_events.jsonl").read_text(encoding="utf-8").splitlines()]

        self.assertFalse(app.exception)
        self.assertEqual(view.identity_status, "registered")
        self.assertFalse(view.import_ready)
        self.assertEqual(view.candidate.suggestions[0].review_status, "pending")
        self.assertTrue(any("suggestion" in item.casefold() for item in remaining_blockers))
        self.assertEqual(sum(item.get("event") == "candidate_identity_link"
                             and item.get("candidate_id") == "held-identity-v2" for item in events), 1)

    def test_identity_panel_batch_count_and_confirmation_use_only_safe_matches(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = write_ui_fixture(Path(tmp))
            output = Path(fixture["output"])
            (output / "import_state.jsonl").unlink()
            base = next(view for view in load_catalog(output) if view.candidate.name == "Fried Rice")
            store = ReviewStore(output / "review_events.jsonl")
            held_ids = []
            for index in range(17):
                source_hash = f"synthetic-source-{index}"
                recipe = RecipeCandidate(
                    name=f"Synthetic blend {index}", ingredients=[f"{index + 1} tsp salt"],
                    instructions=["Stir."],
                )
                prior = store.register_candidates("synthetic.jpg", source_hash,
                                                  f"old-{index}", [recipe])[0]
                updated = recipe.model_copy(update={"suggestions": [Suggestion(
                    suggestion_id=f"suggestion-{index}", field_path="instructions[0]",
                    proposed_value="Stir thoroughly.", rationale="Synthetic proposal.",
                )]})
                payload = _candidate_payload(updated)
                held_id = f"held-{index}"
                extraction_id = f"new-{index}"
                event = store._base_event("candidate_registration", held_id, source_hash,
                                          extraction_id, f"held-registration-{index}")
                event.update({"source": "synthetic.jpg", "fingerprint": hashlib.sha256(
                    _canonical(payload).encode()).hexdigest(), "identity_status": "needs_reconciliation",
                    "candidate": payload, "legacy_aliases": [], "target_bindings": []})
                with (output / "review_events.jsonl").open("a", encoding="utf-8") as ledger:
                    ledger.write(json.dumps(event) + "\n")
                held_ids.append((held_id, prior["candidate_id"]))
                store = ReviewStore(output / "review_events.jsonl")
            # Add one displayable current candidate; the remaining 17 are queue-level synthetic records.
            updated_visible = base.candidate.model_copy(update={"suggestions": [Suggestion(
                suggestion_id="visible-proposal", field_path="instructions[0]",
                proposed_value="Fold in gently.", rationale="Synthetic proposal.",
            )]})
            extraction_id = extraction_identity(base.source_hash, {
                "recipes": [updated_visible.model_dump(mode="json")], "general_warnings": [],
            })
            payload = _candidate_payload(updated_visible)
            visible_event = store._base_event("candidate_registration", "held-visible", base.source_hash,
                                              extraction_id, "held-visible-registration")
            visible_event.update({"source": str(fixture["source"]), "fingerprint": hashlib.sha256(
                _canonical(payload).encode()).hexdigest(), "identity_status": "needs_reconciliation",
                "candidate": payload, "legacy_aliases": [], "target_bindings": []})
            with (output / "review_events.jsonl").open("a", encoding="utf-8") as ledger:
                ledger.write(json.dumps(visible_event) + "\n")
            (output / "extracted" / f"{base.source_hash}--batch-v2.json").write_text(
                json.dumps({"source_hash": base.source_hash, "batch": {
                    "recipes": [updated_visible.model_dump(mode="json")], "general_warnings": [],
                }}), encoding="utf-8",
            )
            environment = patch.dict(os.environ, {
                "RECIPE_IMPORTER_OUTPUT_DIR": str(output),
                "RECIPE_IMPORTER_SOURCE_DIR": str(Path(fixture["source"]).parent),
            }, clear=False)
            environment.start()
            self.addCleanup(environment.stop)
            app = AppTest.from_file(str(APP_PATH)).run(timeout=15)
            picker = next(item for item in app.selectbox if item.label == "Jump to recipe")
            picker.select("held-visible").run(timeout=15)
            next(button for button in app.button if button.label == "Resolve identity").click().run(timeout=15)
            self.assertTrue(any(button.label == "Confirm all 18 safe matches" for button in app.button))
            next(button for button in app.button if button.label == "Confirm all 18 safe matches").click().run(timeout=15)
            linked = [event for event in ReviewStore(output / "review_events.jsonl").events
                      if event.get("event") == "candidate_identity_link"
                      and event.get("decision") == "confirmed_unique_content_match"]

        self.assertFalse(app.exception)
        self.assertEqual(len(linked), 18)
        self.assertEqual(len(held_ids), 17)

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
