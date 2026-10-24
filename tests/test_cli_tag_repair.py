from __future__ import annotations

import csv
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from import_recipes import StateStore, main, repair_import_tags


class FakeTagMealie:
    def __init__(self):
        self.tags = []
        self.created = []

    def ensure_tag(self, name):
        return {"name": name, "slug": name.casefold().replace(" ", "-")}

    def add_tag(self, slug, tag):
        self.tags.append((slug, tag["name"]))
        return "already_present" if slug == "pending-recipe" else "tagged"

    def create_recipe(self, _recipe):
        self.created.append("called")
        raise AssertionError("tag maintenance must never create recipes")


class CliTagRepairTests(unittest.TestCase):
    def _state(self, output: Path):
        state = StateStore(output / "import_state.jsonl")
        for identity, slug, tag_status, tag in (
            ("pending-id", "pending-recipe", "pending", "Cookbook"),
            ("unknown-id", "unknown-recipe", "unknown", "Cookbook"),
            ("failed-id", "failed-recipe", "failed", "Cookbook"),
            ("other-tag", "other-recipe", "failed", "Other cookbook"),
        ):
            state.add(identity, "IMPORTED", slug, "missing-scan.pdf", identity,
                      identity=identity, batch_tag=tag, tag_name=tag,
                      tag_status=tag_status)

    def test_tag_repair_scopes_pending_unknown_and_failed_without_recipe_creates(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "output"
            output.mkdir()
            self._state(output)
            client = FakeTagMealie()

            rows = repair_import_tags(output, "Cookbook", client)

        self.assertEqual({row["candidate_id"] for row in rows},
                         {"pending-id", "unknown-id", "failed-id"})
        self.assertEqual(client.created, [])
        self.assertEqual(len(client.tags), 3)
        self.assertEqual({row["tag_status"] for row in rows}, {"tagged", "already_tagged"})

    def test_cli_tag_repair_needs_no_gemini_source_or_extraction_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "library"
            output.mkdir()
            self._state(output)
            client = FakeTagMealie()
            argv = ["import_recipes.py", "--output-dir", str(output),
                    "--source", str(Path(tmp) / "missing-source"),
                    "--repair-failed-tags", "--batch-tag", "Cookbook"]
            environment = {
                "GEMINI_API_KEY": "", "MEALIE_TOKEN": "fixture-token",
                "MEALIE_URL": "http://fixture.invalid", "RECIPE_DIR": str(Path(tmp) / "missing-source"),
            }
            with patch.object(sys, "argv", argv), patch.dict(os.environ, environment, clear=True), \
                    patch("import_recipes.load_dotenv"), \
                    patch("import_recipes.preflight_mealie"), \
                    patch("import_recipes.MealieClient", return_value=client), \
                    patch("import_recipes.ModelRouter", side_effect=AssertionError("model router initialized")):
                code = main()
            with (output / "last_run_report.csv").open(encoding="utf-8-sig", newline="") as stream:
                report = list(csv.DictReader(stream))

        self.assertEqual(code, 0)
        self.assertEqual(len(report), 3)
        self.assertEqual(client.created, [])
        self.assertFalse((output / "extracted").exists())

    def test_cli_tag_repair_reports_empty_eligible_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "library"
            output.mkdir()
            argv = ["import_recipes.py", "--output-dir", str(output),
                    "--source", str(Path(tmp) / "missing-source"),
                    "--repair-tags", "--batch-tag", "Cookbook"]
            environment = {"GEMINI_API_KEY": "", "MEALIE_TOKEN": "fixture-token",
                           "MEALIE_URL": "http://fixture.invalid"}
            with patch.object(sys, "argv", argv), patch.dict(os.environ, environment, clear=True), \
                    patch("import_recipes.load_dotenv"), patch("import_recipes.preflight_mealie"), \
                    patch("import_recipes.MealieClient", return_value=FakeTagMealie()):
                code = main()

        self.assertEqual(code, 1)

if __name__ == "__main__":
    unittest.main()
