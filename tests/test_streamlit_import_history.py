import os
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from import_recipes import StateStore
from batch_store import BatchStore
from tests.ui_fixture_factory import write_ui_fixture
from review_store import ReviewStore
from recipe_quality import RecipeCandidate, schema_recipe
from ui_data import load_catalog

PAGE = Path(__file__).resolve().parents[1] / "app_pages" / "import_history.py"
APP_PATH = Path(__file__).resolve().parents[1] / "streamlit_app.py"


class StreamlitImportHistoryTests(unittest.TestCase):
    def test_pending_and_unknown_tag_states_have_recovery_actions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = write_ui_fixture(root)
            output = Path(fixture["output"])
            state = StateStore(output / "import_state.jsonl")
            state.add("pending-tag", "IMPORTED", "pending-slug", "missing.pdf", "Pending Tag",
                      identity="pending-tag", batch_tag="Cookbook", tag_status="pending")
            state.add("unknown-tag", "IMPORTED", "unknown-slug", "missing.pdf", "Unknown Tag",
                      identity="unknown-tag", batch_tag="Cookbook", tag_status="unknown")
            with patch.dict(os.environ, {
                "RECIPE_IMPORTER_OUTPUT_DIR": str(output),
                "RECIPE_IMPORTER_SOURCE_DIR": str(root / "sources"),
                "RECIPE_IMPORTER_FIXTURE_MODE": "1",
            }):
                app = AppTest.from_file(str(PAGE)).run(timeout=15)

        self.assertFalse(app.exception)
        labels = {item.label for item in app.button}
        self.assertIn("Apply pending tag", labels)
        self.assertIn("Verify or repair tag", labels)

    def test_reconciliation_error_without_service_identifiers_is_visible_and_durable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = write_ui_fixture(root)
            output = Path(fixture["output"])
            view = next(item for item in load_catalog(output, root / "sources")
                        if item.candidate.name == "Fried Rice")
            intent = {
                "event": "intent_started", "event_id": "missing-result-intent",
                "attempt_id": "missing-result-attempt", "candidate_id": view.candidate_id,
                "source_hash": view.source_hash, "extraction_id": view.extraction_id,
                "revision": 1, "content_sha256": "synthetic-content-digest",
                "title": view.candidate.name, "source_path": str(fixture["source"]),
                "batch_tag": "", "recipe_snapshot": view.candidate.model_dump(mode="json"),
            }
            with (output / "ui_import_events.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(intent) + "\n")

            with patch.dict(os.environ, {"RECIPE_IMPORTER_OUTPUT_DIR": str(output),
                                         "RECIPE_IMPORTER_SOURCE_DIR": str(root / "sources"),
                                         "RECIPE_IMPORTER_FIXTURE_MODE": "1"}), \
                 patch("app_pages.import_history.ImportService.reconcile_existing",
                       return_value={"creation_status": "not_verified",
                                     "reason": "Provider lookup failed."}):
                app = AppTest.from_file(str(PAGE)).run(timeout=15)
                app.text_input(key=f"slug:{view.candidate_id}").set_value("synthetic-existing").run(timeout=15)
                next(button for button in app.button if button.label == "Inspect existing recipe").click().run(timeout=15)
                self.assertFalse(app.exception)
                self.assertTrue(any("Provider lookup failed." in item.value for item in app.caption),
                                repr(app.caption))

            events = [json.loads(line) for line in
                      (output / "ui_import_events.jsonl").read_text(encoding="utf-8").splitlines()]
            feedback = next(item for item in events
                            if item.get("event") == "reconciliation_action_result")

        self.assertEqual(feedback["attempt_id"], "missing-result-attempt")
        self.assertEqual(feedback["result_status"], "not_verified")
        self.assertTrue(feedback["protection_active"])
        self.assertEqual(feedback["action"], "Inspect existing Mealie recipe")

    def test_orphaned_intent_ui_compares_before_explicit_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = write_ui_fixture(root)
            output = Path(fixture["output"])
            view = next(item for item in load_catalog(output, root / "sources")
                        if item.candidate.name == "Fried Rice")
            store = ReviewStore(output / "review_events.jsonl")
            saved = store.save_revision(
                candidate_id=view.candidate_id, source_hash=view.source_hash,
                extraction_id=view.extraction_id, expected_parent=0,
                operation_id="uncertain-ui-save", recipe=view.candidate.model_dump(mode="json"),
                target_bindings=[],
            )
            store.approve_revision(
                candidate_id=view.candidate_id, source_hash=view.source_hash,
                extraction_id=view.extraction_id, revision=saved["revision"],
                operation_id="uncertain-ui-approve", source_path=fixture["source"],
            )
            from ui_import_service import ImportService
            service = ImportService(output, source_root=root / "sources")
            eligible = service.preview([view.candidate_id])["eligible"][0]
            attempt_id = "synthetic-uncertain-attempt"
            intent = {
                "event": "intent_started", "event_id": "synthetic-intent-event",
                "attempt_id": attempt_id, "candidate_id": view.candidate_id,
                "source_hash": view.source_hash, "extraction_id": view.extraction_id,
                "revision": eligible["revision"], "content_sha256": eligible["content_sha256"],
                "title": eligible["title"], "source_path": eligible["source_path"],
                "batch_tag": "", "recipe_snapshot": eligible["recipe"],
            }
            with (output / "ui_import_events.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(intent) + "\n")
            returned = schema_recipe(RecipeCandidate.model_validate(eligible["recipe"]))
            returned["recipeIngredient"] = ["A different ingredient list"]
            (output / "fixture_mealie.json").write_text(json.dumps({
                "recipes": {"synthetic-existing": {
                    "id": "synthetic-mealie-id", "slug": "synthetic-existing",
                    "name": eligible["title"], "tags": [], "payload": returned,
                }}, "tags": {},
            }), encoding="utf-8")

            with patch.dict(os.environ, {"RECIPE_IMPORTER_OUTPUT_DIR": str(output),
                                         "RECIPE_IMPORTER_SOURCE_DIR": str(root / "sources"),
                                         "RECIPE_IMPORTER_FIXTURE_MODE": "1"}):
                app = AppTest.from_file(str(PAGE)).run(timeout=15)
                self.assertTrue(any("no result was recorded" in item.value for item in app.warning))
                self.assertTrue(any(attempt_id in item.value for item in app.caption))
                app.text_input(key=f"slug:{view.candidate_id}").set_value("synthetic-existing").run(timeout=15)
                next(button for button in app.button if button.label == "Inspect existing recipe").click().run(timeout=15)
                self.assertFalse(app.exception)
                self.assertTrue(any("Compare the approved recipe" in item.value for item in app.markdown))
                self.assertTrue(any("Differences found: ingredients" in item.value for item in app.warning))
                self.assertFalse(any(json.loads(line).get("event") == "reconciled_existing"
                                     for line in (output / "ui_import_events.jsonl").read_text(encoding="utf-8").splitlines()))
                evidence = next(item for item in app.text_area
                                if item.label == "Evidence for linking this Mealie recipe to the uncertain import")
                app.text_area(key=evidence.key).set_value("Compared the returned contents to the exact approved revision.").run(timeout=15)
                ack = next(item for item in app.checkbox
                           if item.label.startswith("I reviewed the recipe content differences"))
                app.checkbox(key=ack.key).check().run(timeout=15)
                unavailable = next(item for item in app.checkbox
                                   if item.label.startswith("I understand the listed fields were not returned"))
                app.checkbox(key=unavailable.key).check().run(timeout=15)
                next(button for button in app.button if button.label == "Confirm this existing recipe").click().run(timeout=15)
                app.run(timeout=15)
                self.assertFalse(app.exception)
                self.assertTrue(any("Existing recipe linked for this attempt" in item.value
                                    for item in app.success),
                                repr(app.session_state.get(f"reconcile_inspection:{view.candidate_id}")))

            events = [json.loads(line) for line in (output / "ui_import_events.jsonl").read_text(encoding="utf-8").splitlines()]
            linked = next(item for item in events if item.get("event") == "reconciled_existing")
            self.assertEqual(linked["attempt_id"], attempt_id)
            self.assertTrue(linked["reviewer_confirmed"])
            self.assertTrue(linked["material_difference_ack"])
            self.assertTrue(linked["unavailable_field_ack"])

    def test_historical_intent_ui_discloses_missing_content_and_requires_acknowledgment(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = write_ui_fixture(root)
            output = Path(fixture["output"])
            view = next(item for item in load_catalog(output, root / "sources")
                        if item.candidate.name == "Fried Rice")
            attempt_id = "historical-cli-ui-attempt"
            intent = {
                "event": "intent_started", "schema_version": 1,
                "attempt_id": attempt_id, "plan_id": "cli",
                "candidate_id": view.candidate_id, "source_hash": view.source_hash,
                "extraction_id": view.extraction_id, "revision": view.revision,
                "title": view.candidate.name, "source_path": str(fixture["source"]),
                "batch_tag": "Historical batch",
            }
            with (output / "ui_import_events.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(intent) + "\n")
                handle.write(json.dumps({"event": "create_uncertain", "attempt_id": attempt_id,
                                         "candidate_id": view.candidate_id}) + "\n")
            (output / "fixture_mealie.json").write_text(json.dumps({
                "recipes": {"historical-existing": {
                    "id": "historical-mealie-id", "slug": "historical-existing",
                    "name": view.candidate.name, "tags": [],
                    "payload": schema_recipe(view.candidate),
                }}, "tags": {},
            }), encoding="utf-8")

            with patch.dict(os.environ, {
                "RECIPE_IMPORTER_OUTPUT_DIR": str(output),
                "RECIPE_IMPORTER_SOURCE_DIR": str(root / "sources"),
                "RECIPE_IMPORTER_FIXTURE_MODE": "1",
            }):
                app = AppTest.from_file(str(PAGE)).run(timeout=15)
                app.text_input(key=f"slug:{view.candidate_id}").set_value("historical-existing").run(timeout=15)
                next(button for button in app.button if button.label == "Inspect existing recipe").click().run(timeout=15)
                self.assertFalse(app.exception)
                self.assertTrue(any("did not preserve the submitted recipe snapshot or immutable content digest" in item.value
                                    for item in app.warning), repr(([getattr(x, "value", "") for x in app.warning],
                                                                    [getattr(x, "value", "") for x in app.caption])))
                confirm = next(button for button in app.button if button.label == "Confirm this existing recipe")
                self.assertTrue(confirm.disabled)
                self.assertTrue(any(item.label == "I understand the original submitted content evidence is unavailable."
                                    for item in app.checkbox))

            events = [json.loads(line) for line in
                      (output / "ui_import_events.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertFalse(any(item.get("event") == "reconciled_existing" for item in events))

    def test_tag_repair_outcome_survives_rerender_until_dismissed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = write_ui_fixture(root)
            output = Path(fixture["output"])
            view = next(item for item in load_catalog(output, root / "sources")
                        if item.candidate.name == "Fried Rice")
            store = ReviewStore(output / "review_events.jsonl")
            saved = store.save_revision(
                candidate_id=view.candidate_id, source_hash=view.source_hash,
                extraction_id=view.extraction_id, expected_parent=0,
                operation_id="tag-repair-ui-save",
                recipe=view.candidate.model_dump(mode="json"), target_bindings=[],
            )
            store.approve_revision(
                candidate_id=view.candidate_id, source_hash=view.source_hash,
                extraction_id=view.extraction_id, revision=saved["revision"],
                operation_id="tag-repair-ui-approve", source_path=fixture["source"],
            )

            class FakeMealie:
                supports_empty_instructions = True
                def __init__(self): self.tag_error = True
                def create_recipe(self, recipe): return "tag-repair-slug"
                def ensure_tag(self, name):
                    if self.tag_error: raise RuntimeError("synthetic tag failure")
                    return {"name": name}
                def add_tag(self, slug, tag): return "tagged"

            client = FakeMealie()
            service = __import__("ui_import_service").ImportService(
                output, source_root=root / "sources", client=client)
            plan = service.preview([view.candidate_id], "Synthetic cookbook")
            self.assertEqual(service.dispatch(plan["plan_id"])[0]["tag_status"], "failed")
            client.tag_error = False

            with patch.dict(os.environ, {"RECIPE_IMPORTER_OUTPUT_DIR": str(output),
                                         "RECIPE_IMPORTER_SOURCE_DIR": str(root / "sources"),
                                         "RECIPE_IMPORTER_FIXTURE_MODE": "1"}), \
                    patch("application_services.configured_mealie_client", return_value=client):
                app = AppTest.from_file(str(PAGE)).run(timeout=15)
                next(button for button in app.button if button.label == "Retry failed tag").click().run(timeout=15)
                self.assertFalse(app.exception)
                self.assertTrue(any("Tag applied" in item.value for item in app.success))
                app.run(timeout=15)
                self.assertTrue(any("Tag applied" in item.value for item in app.success))
                next(button for button in app.button if button.label == "Dismiss recovery result").click().run(timeout=15)
                self.assertFalse(any("Tag applied" in item.value for item in app.success))

    def test_current_batch_tag_defaults_into_selected_import_preflight(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = write_ui_fixture(root)
            output = Path(fixture["output"])
            batch_store = BatchStore(output)
            batch = batch_store.create("Sunday recipes", "Sunday cookbook")
            staged = batch_store.stage(batch["batch_id"], "family-rice-scan.pdf",
                                       Path(fixture["source"]).read_bytes())
            self.assertEqual(staged["status"], "staged")
            view = next(item for item in load_catalog(output, root / "sources")
                        if item.candidate.name == "Fried Rice")
            self.assertEqual(view.source_display_name, "family-rice-scan.pdf")
            self.assertEqual(view.batch_name, "Sunday recipes")
            self.assertEqual(view.batch_tag, "Sunday cookbook")
            store = ReviewStore(output / "review_events.jsonl")
            saved = store.save_revision(
                candidate_id=view.candidate_id, source_hash=view.source_hash,
                extraction_id=view.extraction_id, expected_parent=0,
                operation_id="batch-tag-approved-edit",
                recipe=view.candidate.model_dump(mode="json"), target_bindings=[],
            )
            store.approve_revision(
                candidate_id=view.candidate_id, source_hash=view.source_hash,
                extraction_id=view.extraction_id, revision=saved["revision"],
                operation_id="batch-tag-approved-consent", source_path=fixture["source"],
            )
            with patch.dict(os.environ, {"RECIPE_IMPORTER_OUTPUT_DIR": str(output),
                                         "RECIPE_IMPORTER_SOURCE_DIR": str(root / "sources"),
                                         "RECIPE_IMPORTER_FIXTURE_MODE": "1"}):
                review_app = AppTest.from_file(str(APP_PATH)).run(timeout=15)
                self.assertFalse(review_app.exception)
                self.assertTrue(any("family-rice-scan.pdf" in item.value
                                    and "Sunday recipes" in item.value
                                    for item in review_app.caption))
                app = AppTest.from_file(str(PAGE)).run(timeout=15)
                app.multiselect[0].set_value([view.candidate_id]).run(timeout=15)
                tag_input = next(item for item in app.text_input if item.label == "Mealie tag (optional)")
                self.assertEqual(tag_input.value, "Sunday cookbook")
                next(button for button in app.button if button.label == "Preview import").click().run(timeout=15)
                self.assertFalse(app.exception)
                self.assertEqual(app.session_state["import_plan"]["batch_tag"], "Sunday cookbook")

    def test_selected_exact_revision_import_uses_fake_service_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = write_ui_fixture(root)
            output = Path(fixture["output"])
            view = next(item for item in load_catalog(output, root / "sources") if item.candidate.name == "Fried Rice")
            store = ReviewStore(output / "review_events.jsonl")
            recipe = view.candidate.model_copy(deep=True)
            recipe.ingredients[0] = "3 cups rice"
            saved = store.save_revision(candidate_id=view.candidate_id, source_hash=view.source_hash,
                                        extraction_id=view.extraction_id, expected_parent=0,
                                        operation_id="synthetic-save", recipe=recipe.model_dump(mode="json"),
                                        target_bindings=[])
            store.approve_revision(candidate_id=view.candidate_id, source_hash=view.source_hash,
                                   extraction_id=view.extraction_id, revision=saved["revision"],
                                   operation_id="synthetic-approve", source_path=fixture["source"])
            with patch.dict(os.environ, {"RECIPE_IMPORTER_OUTPUT_DIR": str(output),
                                         "RECIPE_IMPORTER_SOURCE_DIR": str(root / "sources"),
                                         "RECIPE_IMPORTER_FIXTURE_MODE": "1"}):
                app = AppTest.from_file(str(PAGE)).run(timeout=15)
                app.multiselect[0].set_value([view.candidate_id]).run(timeout=15)
                next(button for button in app.button if button.label == "Preview import").click().run(timeout=15)
                next(button for button in app.button if button.label == "Import 1 recipe").click().run(timeout=15)
                self.assertFalse(app.exception)
                app.run(timeout=15)
            import json
            fake_recipes = json.loads((output / "fixture_mealie.json").read_text(encoding="utf-8"))["recipes"]
            self.assertEqual(len(fake_recipes), 1)
            self.assertEqual(next(iter(fake_recipes.values()))["payload"]["recipeIngredient"][0], "3 cups rice")

    def test_import_selection_starts_empty_and_no_client_is_constructed(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = write_ui_fixture(Path(tmp))
            with patch.dict(os.environ, {"RECIPE_IMPORTER_OUTPUT_DIR": str(fixture["output"]),
                                         "RECIPE_IMPORTER_SOURCE_DIR": str(Path(tmp) / "sources"),
                                         "RECIPE_IMPORTER_FIXTURE_MODE": "1"}):
                with patch("application_services.configured_mealie_client") as client:
                    app = AppTest.from_file(str(PAGE)).run(timeout=15)
                self.assertFalse(app.exception)
                self.assertEqual(next(item for item in app.multiselect if item.label == "Recipes to import").value, [])
                client.assert_not_called()

    def test_history_survives_without_extraction_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            StateStore(output / "import_state.jsonl").add(
                "synthetic-candidate", "IMPORTED", "synthetic-rice", "synthetic.jpg", "Synthetic rice",
                identity="synthetic-candidate", tag_status="failed", tag_name="Synthetic cookbook")
            with patch.dict(os.environ, {"RECIPE_IMPORTER_OUTPUT_DIR": str(output),
                                         "RECIPE_IMPORTER_SOURCE_DIR": ""}):
                app = AppTest.from_file(str(PAGE)).run(timeout=15)
            self.assertFalse(app.exception)
            self.assertTrue(any("Synthetic rice" in item.value for item in app.markdown))
            self.assertTrue(any("tag" in button.label.lower() for button in app.button))
