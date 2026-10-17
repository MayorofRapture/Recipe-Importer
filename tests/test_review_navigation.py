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
from app_pages.common import buffer_for, save_draft
from review_store import ReviewStore


APP_PATH = Path(__file__).resolve().parents[1] / "streamlit_app.py"


class ReviewNavigationTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parents[1] / f".test-review-navigation-{uuid.uuid4().hex}"
        self.root.mkdir()
        self.addCleanup(lambda: shutil.rmtree(self.root, ignore_errors=True))
        self.fixture = write_ui_fixture(self.root)
        self.env = patch.dict(os.environ, {
            "RECIPE_IMPORTER_OUTPUT_DIR": str(self.fixture["output"]),
            "RECIPE_IMPORTER_SOURCE_DIR": str(self.root / "sources"),
        }, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)

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
        next(item for item in app.button if item.label == "Back").click().run(timeout=10)
        self.assertEqual(app.session_state["review_selection"], fried_rice)
        self.assertFalse(app.exception)
        self.assertFalse(app.exception)


if __name__ == "__main__":
    unittest.main()
