import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from import_recipes import StateStore, candidate_identity, file_digest
from draft_store import DraftStore
from recipe_quality import ExtractionBatch, RecipeCandidate
from review_store import ReviewStore
from streamlit.testing.v1 import AppTest


APP_PATH = Path(__file__).resolve().parents[1] / "streamlit_app.py"


def _library(root: Path, imported: bool = False) -> tuple[Path, str]:
    output = root / "output"
    extracted = output / "extracted"
    sources = root / "sources"
    extracted.mkdir(parents=True)
    sources.mkdir()
    source = sources / "raspberry.jpg"
    source.write_bytes(b"synthetic recipe image")
    digest = file_digest(source)
    recipe = RecipeCandidate(
        name="Raspberry Cream Center",
        ingredients=[
            "Combine 2 cups powdered sugar, 1/2 cup (1 stick) softened butter or margarine and 2 tablespoons raspberry-flavored liqueur* in small bowl; beat until smooth.",
            "*1/4 cup raspberry preserves and 1 teaspoon water may be substituted for the raspberry-flavored liqueur.",
        ],
    )
    batch = ExtractionBatch(recipes=[recipe])
    (extracted / f"{digest}.json").write_text(json.dumps({
        "schema_version": 2, "source_hash": digest, "batch": batch.model_dump(mode="json"),
    }), encoding="utf-8")
    if imported:
        StateStore(output / "import_state.jsonl").add(
            candidate_identity(digest, recipe), "IMPORTED", "raspberry-cream-center",
            str(source), recipe.name,
        )
    return output, digest


class StreamlitRecipeQualityTests(unittest.TestCase):
    def _app(self, root: str, queue: str = "Needs action", imported: bool = False) -> tuple[AppTest, Path, str]:
        output, digest = _library(Path(root), imported=imported)
        env = patch.dict(os.environ, {
            "RECIPE_IMPORTER_OUTPUT_DIR": str(output),
            "RECIPE_IMPORTER_SOURCE_DIR": str(Path(root, "sources")),
        }, clear=False)
        env.start()
        self.addCleanup(env.stop)
        app = AppTest.from_file(str(APP_PATH))
        app.session_state["review_queue"] = queue
        return app.run(timeout=12), output, digest

    def test_review_shows_warning_and_applies_one_explicit_saved_repair(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, output, digest = self._app(tmp)
            self.assertFalse(app.exception)
            self.assertTrue(any("preparation instructions or substitution notes" in item.value
                                for item in app.warning))
            next(button for button in app.button if button.label == "Review ingredient extraction").click().run(timeout=12)
            next(button for button in app.button if button.label == "Apply proposed repair").click().run(timeout=12)

            self.assertFalse(app.exception)
            store = ReviewStore(output / "review_events.jsonl")
            candidate_id = next(iter(store.registrations))
            revisions = store.revisions[candidate_id]
            recipe = revisions[-1]["recipe"]

        self.assertEqual(recipe["ingredients"], [
            "2 cups powdered sugar", "1/2 cup (1 stick) softened butter or margarine",
            "2 tablespoons raspberry-flavored liqueur*",
        ])
        self.assertIn("Combine 2 cups powdered sugar", recipe["instructions"][0])
        self.assertIn("may be substituted", recipe["notes"][0])
        self.assertEqual(revisions[-1]["source_hash"], digest)
        self.assertFalse(any(event.get("event") == "review_approval" for event in store.events))

    def test_extraction_quality_queue_is_discoverable_and_shows_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, _output, _digest = self._app(tmp, queue="Extraction quality")

        self.assertFalse(app.exception)
        self.assertTrue(any(item.label == "Recipes inspected" for item in app.metric))
        self.assertTrue(any(item.label == "Potential issues" for item in app.metric))
        self.assertTrue(any(button.label == "Review ingredient extraction" for button in app.button))
        self.assertFalse((Path(tmp) / "output" / "review_events.jsonl").exists())

    def test_repair_edits_survive_app_restart_without_changing_recipe_revision(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, output, _digest = self._app(tmp)
            next(button for button in app.button if button.label == "Review ingredient extraction").click().run(timeout=12)
            next(item for item in app.text_area if item.label == "Proposed notes and substitutions").set_value(
                "Optional raspberry preserves replacement."
            )
            next(button for button in app.button if button.label == "Save repair draft").click().run(timeout=12)

            restored = DraftStore(output / "ui_drafts.jsonl").latest(app.session_state["review_selection"])
            reopened = AppTest.from_file(str(APP_PATH)).run(timeout=12)
            next(button for button in reopened.button if button.label == "Review ingredient extraction").click().run(timeout=12)

        self.assertEqual(restored.repair_proposal["proposal"]["notes"], [
            "Optional raspberry preserves replacement.",
        ])
        self.assertEqual(len(ReviewStore(output / "review_events.jsonl").revisions.get(
            app.session_state["review_selection"], [])), 0)
        self.assertEqual(next(item for item in reopened.text_area
                              if item.label == "Proposed notes and substitutions").value,
                         "Optional raspberry preserves replacement.")

    def test_cancelled_repair_leaves_recipe_and_review_history_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, output, _digest = self._app(tmp)
            candidate_id = app.session_state["review_selection"]
            next(button for button in app.button if button.label == "Review ingredient extraction").click().run(timeout=12)
            next(button for button in app.button if button.label == "Cancel").click().run(timeout=12)
            store = ReviewStore(output / "review_events.jsonl")

        self.assertEqual(store.revisions.get(candidate_id, []), [])
        self.assertFalse(any(event.get("event") == "review_approval" for event in store.events))

    def test_imported_recipe_is_read_only_in_quality_audit(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, _output, _digest = self._app(tmp, queue="All recipes", imported=True)

        repair = next(button for button in app.button if button.label == "Review ingredient extraction")
        self.assertTrue(repair.disabled)
        self.assertTrue(any("Read-only" in item.value for item in app.warning))


if __name__ == "__main__":
    unittest.main()
