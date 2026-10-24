from __future__ import annotations

import unittest
import os
import hashlib
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import fitz

from import_recipes import file_digest
from recipe_quality import ExtractionBatch, RecipeCandidate
from review_store import ReviewConflict, ReviewStore, extraction_identity
from ui_import_service import ImportService
from ui_data import load_catalog
from tests.p0_fixture_factory import write_p0_fixture, write_p0_source_groups


class P0FixtureSafetyTests(unittest.TestCase):
    def _temporary_root(self) -> Path:
        temporary_root = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_root.cleanup)
        return Path(temporary_root.name)

    def test_populated_fixture_loads_and_keeps_duplicate_identity_ambiguous(self):
        root = self._temporary_root() / "p0-fixture-unittest"
        fixture = write_p0_fixture(root)
        self.assertTrue((fixture["output"] / ".fixture-library").exists())
        self.assertEqual(len(fixture["expected"]), 500)
        self.assertGreaterEqual(fixture["history_count"], 300)
        self.assertGreater(fixture["source"].stat().st_size, 1_000_000)

        views = load_catalog(fixture["output"], fixture["source_root"])

        self.assertEqual(len(views), 500)
        self.assertEqual(len({view.candidate_id for view in views}), 500)
        duplicates = [view for view in views if view.candidate.name == "Duplicate Test Recipe"]
        self.assertEqual(len(duplicates), 2)
        self.assertEqual(len({view.candidate_id for view in duplicates}), 2)
        self.assertEqual(sum(view.identity_status == "registered" for view in views), 500)
        self.assertEqual(sum(view.identity_status == "needs_reconciliation" for view in views), 0)
        self.assertTrue(any(view.status == "Imported" for view in views))
        self.assertTrue(any(view.status != "Imported" for view in views))
        self.assertEqual(sum(view.revision >= 2 for view in views), 10)
        self.assertEqual(sum(view.draft is not None for view in views), 50)
        approved = next(view for view in views if view.candidate_id == fixture["registered_ids"][300])
        self.assertTrue(approved.import_ready)

    def test_fixture_rejects_unrealistic_recipe_counts(self):
        with self.assertRaises(ValueError):
            write_p0_fixture(self._temporary_root() / "p0-invalid", recipe_count=20)

    def test_mixed_source_groups_resolve_available_sources_and_keep_missing_sources_unavailable(self):
        fixture = write_p0_source_groups(self._temporary_root() / "p0-source-groups")
        views = load_catalog(fixture["output"], fixture["source_root"])
        self.assertEqual(len(views), 500)
        self.assertEqual(sum(view.source_available for view in views), 450)
        self.assertEqual(sum(not view.source_available for view in views), 50)
        self.assertEqual(len({Path(item["source"]).parent.name for item in fixture["groups"]}), 8)

    def test_cached_display_hash_cannot_authorize_new_approval_or_import_after_same_stat_tamper(self):
        root = self._temporary_root() / "p0-boundary-tamper"
        output, sources = root / "output", root / "sources"
        extracted = output / "extracted"
        extracted.mkdir(parents=True, exist_ok=True)
        sources.mkdir(parents=True, exist_ok=True)
        (output / ".fixture-library").write_text("synthetic P0 integrity fixture", encoding="utf-8")
        for path in extracted.glob("*.json"):
            path.unlink()
        source = sources / "tamper-check.pdf"
        document = fitz.open()
        page = document.new_page()
        page.insert_text((72, 72), "Synthetic source integrity test.")
        document.save(source)
        document.close()
        digest = file_digest(source)
        candidate = RecipeCandidate(name="Synthetic Tamper Test", ingredients=["1 cup oats"],
                                    instructions=["Mix and bake."])
        batch = ExtractionBatch(recipes=[candidate])
        extraction_id = extraction_identity(digest, batch.model_dump(mode="json"))
        (extracted / f"{digest}.json").write_text(json.dumps({
            "schema_version": 2, "source_hash": digest,
            "batch": batch.model_dump(mode="json"),
        }), encoding="utf-8")
        (output / "review_events.jsonl").write_text("", encoding="utf-8")
        (output / "import_state.jsonl").write_text("", encoding="utf-8")
        (output / "ui_import_events.jsonl").write_text("", encoding="utf-8")

        store = ReviewStore(output / "review_events.jsonl")
        store.register_candidates(str(source), digest, extraction_id, batch.recipes)
        view = load_catalog(output, sources)[0]
        registration = store.registrations[view.candidate_id][-1]
        first = store.save_revision(
            candidate_id=view.candidate_id, source_hash=digest, extraction_id=extraction_id,
            expected_parent=0, operation_id="p0-tamper-revision-1",
            recipe=candidate.model_dump(mode="json"),
            target_bindings=registration["target_bindings"],
        )
        store.approve_revision(
            candidate_id=view.candidate_id, source_hash=digest, extraction_id=extraction_id,
            revision=first["revision"], operation_id="p0-tamper-approval-1", source_path=source,
        )
        before = source.stat()
        bytes_before = source.read_bytes()
        source.write_bytes(bytes_before[:-1] + bytes([bytes_before[-1] ^ 1]))
        os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertEqual(source.stat().st_size, before.st_size)
        self.assertEqual(source.stat().st_mtime_ns, before.st_mtime_ns)
        self.assertNotEqual(file_digest(source), digest)

        # Force the display-only digest cache to return its prior value while
        # leaving approval and import verification on their real byte readers.
        with patch("ui_data._cached_source_digest", return_value=digest):
            stale_display = load_catalog(output, sources)[0]
        self.assertTrue(stale_display.source_available,
                        f"cached display failed for {stale_display.source_path!r}; actual={file_digest(source)} expected={digest}")
        second = store.save_revision(
            candidate_id=view.candidate_id, source_hash=digest, extraction_id=extraction_id,
            expected_parent=1, operation_id="p0-tamper-revision-2",
            recipe=candidate.model_dump(mode="json"),
            target_bindings=registration["target_bindings"],
        )
        with self.assertRaisesRegex(ReviewConflict, "source scan bytes"):
            store.approve_revision(
                candidate_id=view.candidate_id, source_hash=digest, extraction_id=extraction_id,
                revision=second["revision"], operation_id="p0-tamper-approval-2", source_path=source,
            )

        preview = ImportService(output, sources).preview([view.candidate_id])
        self.assertEqual(preview["eligible"], [])
        self.assertEqual(preview["excluded"][0]["reason"], "Source scan is unavailable or no longer matches.")
        decisions = [json.loads(line) for line in (output / "review_events.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(sum(item.get("event") == "review_approval" for item in decisions), 1)


if __name__ == "__main__":
    unittest.main()
