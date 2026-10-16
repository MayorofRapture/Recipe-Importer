import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from import_recipes import StateStore
from batch_store import BatchStore
from tests.ui_fixture_factory import write_ui_fixture
from review_store import ReviewStore
from ui_data import load_catalog

PAGE = Path(__file__).resolve().parents[1] / "app_pages" / "import_history.py"
APP_PATH = Path(__file__).resolve().parents[1] / "streamlit_app.py"


class StreamlitImportHistoryTests(unittest.TestCase):
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
