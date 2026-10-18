from __future__ import annotations

import os
import csv
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
    def __init__(self, tag_result="tagged", create_error=None):
        self.created = []
        self.tag_result = tag_result
        self.create_error = create_error

    def create_recipe(self, recipe):
        self.created.append(recipe.model_dump(mode="json"))
        if self.create_error:
            raise self.create_error
        return "approved-recipe"

    def ensure_tag(self, name):
        return {"name": name, "slug": name.casefold().replace(" ", "-")}

    def add_tag(self, _slug, _tag):
        if self.tag_result in {"failed", "unknown"}:
            raise TimeoutError("synthetic tag timeout") if self.tag_result == "unknown" else RuntimeError("synthetic tag failure")
        return self.tag_result


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
        self.assertEqual(second_code, 0)
        self.assertEqual(len(client.created), 1)
        self.assertEqual(client.created[0]["instructions"], [])

    def test_approved_cli_reports_excluded_candidates_and_tag_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = write_ui_fixture(root)
            output, source = Path(fixture["output"]), Path(fixture["source"])
            view = next(item for item in load_catalog(output, source.parent)
                        if item.candidate.name == "Fried Rice")
            reviews = ReviewStore(output / "review_events.jsonl")
            saved = reviews.save_revision(
                candidate_id=view.candidate_id, source_hash=view.source_hash,
                extraction_id=view.extraction_id, expected_parent=0,
                operation_id="cli-partial-report-save",
                recipe=view.candidate.model_dump(mode="json"), target_bindings=[],
            )
            reviews.approve_revision(
                candidate_id=view.candidate_id, source_hash=view.source_hash,
                extraction_id=view.extraction_id, revision=saved["revision"],
                operation_id="cli-partial-report-approve", source_path=source,
            )
            client = FakeImportMealie(tag_result="failed")
            argv = ["import_recipes.py", "--output-dir", str(output), "--source",
                    str(source.parent), "--import-approved", "--candidate-ids",
                    f"{view.candidate_id},missing-candidate", "--batch-tag", "Synthetic tag"]
            environment = {"GEMINI_API_KEY": "", "MEALIE_TOKEN": "fixture-token",
                           "MEALIE_URL": "http://fixture.invalid",
                           "RECIPE_IMPORTER_OUTPUT_DIR": str(output)}
            with patch.object(sys, "argv", argv), patch.dict(os.environ, environment, clear=True), \
                    patch("import_recipes.load_dotenv"), patch("import_recipes.preflight_mealie"), \
                    patch("import_recipes.MealieClient", return_value=client):
                code = main()
            with (output / "last_run_report.csv").open(encoding="utf-8-sig", newline="") as fh:
                rows = list(csv.DictReader(fh))

        self.assertEqual(code, 1)
        self.assertEqual({row["candidate_id"] for row in rows}, {view.candidate_id, "missing-candidate"})
        created = next(row for row in rows if row["candidate_id"] == view.candidate_id)
        excluded = next(row for row in rows if row["candidate_id"] == "missing-candidate")
        self.assertEqual(created["creation_status"], "created")
        self.assertIn(created["tag_status"], {"failed", "unknown"})
        self.assertEqual(excluded["creation_status"], "excluded")
        self.assertTrue(excluded["note"])

    def test_approved_cli_returns_nonzero_for_unknown_tag_update(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = write_ui_fixture(root)
            output, source = Path(fixture["output"]), Path(fixture["source"])
            view = next(item for item in load_catalog(output, source.parent)
                        if item.candidate.name == "Fried Rice")
            reviews = ReviewStore(output / "review_events.jsonl")
            saved = reviews.save_revision(
                candidate_id=view.candidate_id, source_hash=view.source_hash,
                extraction_id=view.extraction_id, expected_parent=0,
                operation_id="cli-unknown-tag-save", recipe=view.candidate.model_dump(mode="json"),
                target_bindings=[],
            )
            reviews.approve_revision(
                candidate_id=view.candidate_id, source_hash=view.source_hash,
                extraction_id=view.extraction_id, revision=saved["revision"],
                operation_id="cli-unknown-tag-approve", source_path=source,
            )
            client = FakeImportMealie(tag_result="unknown")
            argv = ["import_recipes.py", "--output-dir", str(output), "--source",
                    str(source.parent), "--import-approved", "--candidate-ids",
                    view.candidate_id, "--batch-tag", "Synthetic tag"]
            environment = {"GEMINI_API_KEY": "", "MEALIE_TOKEN": "fixture-token",
                           "MEALIE_URL": "http://fixture.invalid",
                           "RECIPE_IMPORTER_OUTPUT_DIR": str(output)}
            with patch.object(sys, "argv", argv), patch.dict(os.environ, environment, clear=True), \
                    patch("import_recipes.load_dotenv"), patch("import_recipes.preflight_mealie"), \
                    patch("import_recipes.MealieClient", return_value=client):
                code = main()
            with (output / "last_run_report.csv").open(encoding="utf-8-sig", newline="") as fh:
                rows = list(csv.DictReader(fh))

        self.assertEqual(code, 1)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["creation_status"], "created")
        self.assertEqual(rows[0]["tag_status"], "unknown")
        self.assertEqual(rows[0]["outcome_status"], "tag_incomplete")

    def test_approved_cli_writes_report_when_every_candidate_is_excluded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = write_ui_fixture(root)
            output, source = Path(fixture["output"]), Path(fixture["source"])
            view = next(item for item in load_catalog(output, source.parent)
                        if item.candidate.name == "Fried Rice")
            client = FakeImportMealie()
            argv = ["import_recipes.py", "--output-dir", str(output), "--source",
                    str(source.parent), "--import-approved", "--candidate-ids",
                    f"{view.candidate_id},missing-candidate"]
            environment = {"GEMINI_API_KEY": "", "MEALIE_TOKEN": "fixture-token",
                           "MEALIE_URL": "http://fixture.invalid",
                           "RECIPE_IMPORTER_OUTPUT_DIR": str(output)}
            with patch.object(sys, "argv", argv), patch.dict(os.environ, environment, clear=True), \
                    patch("import_recipes.load_dotenv"), patch("import_recipes.preflight_mealie"), \
                    patch("import_recipes.MealieClient", return_value=client):
                code = main()
            with (output / "last_run_report.csv").open(encoding="utf-8-sig", newline="") as fh:
                rows = list(csv.DictReader(fh))

        self.assertEqual(code, 1)
        self.assertEqual({row["candidate_id"] for row in rows}, {view.candidate_id, "missing-candidate"})
        self.assertTrue(all(row["outcome_status"] == "excluded" for row in rows))
        self.assertEqual(client.created, [])

    def test_approved_cli_reports_uncertain_create_and_repeat_does_not_post_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = write_ui_fixture(root)
            output, source = Path(fixture["output"]), Path(fixture["source"])
            view = next(item for item in load_catalog(output, source.parent)
                        if item.candidate.name == "Fried Rice")
            reviews = ReviewStore(output / "review_events.jsonl")
            saved = reviews.save_revision(
                candidate_id=view.candidate_id, source_hash=view.source_hash,
                extraction_id=view.extraction_id, expected_parent=0,
                operation_id="cli-uncertain-save", recipe=view.candidate.model_dump(mode="json"),
                target_bindings=[],
            )
            reviews.approve_revision(
                candidate_id=view.candidate_id, source_hash=view.source_hash,
                extraction_id=view.extraction_id, revision=saved["revision"],
                operation_id="cli-uncertain-approve", source_path=source,
            )
            client = FakeImportMealie(create_error=TimeoutError("synthetic create timeout"))
            argv = ["import_recipes.py", "--output-dir", str(output), "--source",
                    str(source.parent), "--import-approved", "--candidate-ids", view.candidate_id]
            environment = {"GEMINI_API_KEY": "", "MEALIE_TOKEN": "fixture-token",
                           "MEALIE_URL": "http://fixture.invalid",
                           "RECIPE_IMPORTER_OUTPUT_DIR": str(output)}
            codes = []
            with patch.object(sys, "argv", argv), patch.dict(os.environ, environment, clear=True), \
                    patch("import_recipes.load_dotenv"), patch("import_recipes.preflight_mealie"), \
                    patch("import_recipes.MealieClient", return_value=client):
                codes.append(main())
                codes.append(main())
            with (output / "last_run_report.csv").open(encoding="utf-8-sig", newline="") as fh:
                rows = list(csv.DictReader(fh))

        self.assertEqual(codes, [1, 1])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["creation_status"], "uncertain")
        self.assertEqual(rows[0]["outcome_status"], "uncertain")
        self.assertEqual(len(client.created), 1)


if __name__ == "__main__":
    unittest.main()
