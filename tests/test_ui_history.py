from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from import_recipes import StateStore
from ui_import_service import ImportService


class UiHistoryTests(unittest.TestCase):
    def test_tag_timeout_is_unknown_then_read_before_write_repair_finishes(self):
        class FakeMealie:
            def __init__(self):
                self.add_calls = 0
            def ensure_tag(self, name):
                return {"name": name, "slug": "batch"}
            def add_tag(self, _slug, _tag):
                self.add_calls += 1
                if self.add_calls == 1:
                    raise TimeoutError("synthetic timeout after possible tag update")
                return "already_present"
            def create_recipe(self, _recipe):
                raise AssertionError("tag repair cannot create recipes")

        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "output"
            output.mkdir()
            StateStore(output / "import_state.jsonl").add(
                "uncertain-tag", "IMPORTED", "existing-slug", "missing.pdf", "Old Recipe",
                identity="uncertain-tag", batch_tag="Original Tag", tag_status="pending",
            )
            client = FakeMealie()
            service = ImportService(output, client=client)
            unknown = service.repair_tag("uncertain-tag")
            after_restart = ImportService(output, client=client).repair_tag("uncertain-tag")
            rows = ImportService(output).history()

        self.assertEqual(unknown["tag_status"], "unknown")
        self.assertIn("outcome is unknown", unknown["tag_reason"])
        self.assertEqual(after_restart["tag_status"], "already_tagged")
        self.assertEqual(client.add_calls, 2)
        self.assertEqual(rows[0]["tag_status"], "tagged")

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

    def test_conflicting_durable_creation_identities_block_tag_repair(self):
        class FakeMealie:
            def __init__(self): self.tag_calls = 0
            def ensure_tag(self, name):
                self.tag_calls += 1
                return {"name": name}
            def add_tag(self, _slug, _tag):
                self.tag_calls += 1
                return "tagged"
            def create_recipe(self, _recipe):
                raise AssertionError("tag repair cannot create recipes")

        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "output"
            output.mkdir()
            events = output / "ui_import_events.jsonl"
            events.write_text("\n".join(json.dumps({
                "event": "creation_confirmed", "candidate_id": "conflicting-id",
                "attempt_id": f"attempt-{index}", "slug": slug,
                "batch_tag": "Original Tag", "title": "Old Recipe",
            }) for index, slug in enumerate(("recipe-one", "recipe-two"))) + "\n",
                encoding="utf-8")
            client = FakeMealie()
            result = ImportService(output, client=client).repair_tag("conflicting-id")

        self.assertEqual(result["creation_status"], "not_available")
        self.assertIn("conflicting", result["reason"])
        self.assertEqual(client.tag_calls, 0)


if __name__ == "__main__":
    unittest.main()
