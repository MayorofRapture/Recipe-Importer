from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from import_recipes import StateStore
from ui_import_service import ImportService


class UiHistoryTests(unittest.TestCase):
    def test_history_includes_legacy_import_when_extraction_cache_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "output"
            output.mkdir()
            StateStore(output / "import_state.jsonl").add(
                "legacy-candidate", "IMPORTED", "old-recipe-slug", "missing-scan.pdf",
                "Old Recipe", identity="legacy-candidate", tag_status="tagged",
            )

            rows = ImportService(output).history()

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["candidate_id"], "legacy-candidate")
        self.assertEqual(rows[0]["slug"], "old-recipe-slug")
        self.assertEqual(rows[0]["title"], "Old Recipe")
        self.assertFalse(rows[0]["cache_available"])

    def test_legacy_tag_failure_keeps_original_tag_for_repair(self):
        class FakeMealie:
            def __init__(self):
                self.created = []
                self.tags = []
            def create_recipe(self, recipe):
                self.created.append(recipe)
            def ensure_tag(self, name):
                return {"name": name, "slug": "original-tag"}
            def add_tag(self, slug, tag):
                self.tags.append((slug, tag["name"]))
                return "tagged"

        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "output"
            output.mkdir()
            StateStore(output / "import_state.jsonl").add(
                "legacy-candidate", "IMPORTED", "existing-slug", "scan.pdf", "Old Recipe",
                identity="legacy-candidate", tag_status="failed", tag_name="Original Tag",
            )
            client = FakeMealie()
            service = ImportService(output, client=client)
            rows = service.history()
            result = service.repair_tag("legacy-candidate")

        self.assertEqual(rows[0]["batch_tag"], "Original Tag")
        self.assertEqual(result["tag_status"], "tagged")
        self.assertEqual(client.tags, [("existing-slug", "Original Tag")])
        self.assertEqual(client.created, [])


if __name__ == "__main__":
    unittest.main()
