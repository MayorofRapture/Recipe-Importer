from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from import_recipes import main
from recipe_quality import RecipeCandidate
from review_store import ReviewStore
from tests.ui_fixture_factory import write_ui_fixture
from ui_data import load_catalog
from ui_import_service import ImportService


class FakeImportMealie:
    def __init__(self):
        self.created = []

    def create_recipe(self, recipe):
        self.created.append(recipe.model_dump(mode="json"))
        return "approved-recipe"

    def ensure_tag(self, name):
        return {"name": name, "slug": name.casefold().replace(" ", "-")}

    def add_tag(self, _slug, _tag):
        return "tagged"


class CliApprovedImportTests(unittest.TestCase):
    def test_approved_cli_uses_review_history_without_source_or_extraction_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = write_ui_fixture(root)
            output, source = Path(fixture["output"]), Path(fixture["source"])
            view = next(item for item in load_catalog(output, source.parent)
                        if item.candidate.name == "Fried Rice")
            recipe = RecipeCandidate(
                name="Dry Seasoning Blend", ingredients=["1 tsp oregano", "1 tsp thyme"],
                instructions=[],
            )
            reviews = ReviewStore(output / "review_events.jsonl")
            saved = reviews.save_revision(
                candidate_id=view.candidate_id, source_hash=view.source_hash,
                extraction_id=view.extraction_id, expected_parent=0,
                operation_id="missing-source-approved-recipe",
                recipe=recipe.model_dump(mode="json"), target_bindings=[],
            )
            reviews.record_no_instructions_decision(
                candidate_id=view.candidate_id, source_hash=view.source_hash,
                extraction_id=view.extraction_id, revision=saved["revision"],
                reason="Dry ingredient blend", operation_id="missing-source-no-steps",
            )
            reviews.approve_revision(
                candidate_id=view.candidate_id, source_hash=view.source_hash,
                extraction_id=view.extraction_id, revision=saved["revision"],
                operation_id="missing-source-manual-approval", source_path=source,
                approval_method="manual_source_unavailable", reviewer_acknowledged=True,
                review_rationale="Reviewed the saved recipe without its original scan.",
            )
            cache = output / "extracted" / f"{view.source_hash}.json"
            if cache.exists():
                cache.unlink()
            source.unlink()
            disabled = ImportService(output, supports_empty_instructions=False).preview([view.candidate_id])
            self.assertEqual(disabled["eligible"], [])
            self.assertIn("disabled", disabled["excluded"][0]["reason"])
            gui_plan = ImportService(output, supports_empty_instructions=True).preview([view.candidate_id])
            self.assertEqual(len(gui_plan["eligible"]), 1)

            client = FakeImportMealie()
            argv = ["import_recipes.py", "--output-dir", str(output), "--source",
                    str(root / "missing-sources"), "--import-approved",
                    "--candidate-ids", view.candidate_id]
            environment = {
                "GEMINI_API_KEY": "", "MEALIE_TOKEN": "fixture-token",
                "MEALIE_URL": "http://fixture.invalid", "MEALIE_SUPPORTS_EMPTY_INSTRUCTIONS": "true",
            }
            with patch.object(sys, "argv", argv), patch.dict(os.environ, environment, clear=True), \
                    patch("import_recipes.load_dotenv"), \
                    patch("import_recipes.preflight_mealie"), \
                    patch("import_recipes.MealieClient", return_value=client), \
                    patch("import_recipes.ModelRouter", side_effect=AssertionError("model router initialized")):
                code = main()
                second_code = main()

        self.assertEqual(code, 0)
        self.assertEqual(second_code, 1)
        self.assertEqual(len(client.created), 1)
        self.assertEqual(client.created[0]["instructions"], [])


if __name__ == "__main__":
    unittest.main()
