import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import ui_data
from import_recipes import file_digest
from recipe_quality import ExtractionBatch, RecipeCandidate
from ui_data import _SourceIndex, _source_for, _source_for_display, load_catalog, verify_selected_source
from tests.ui_fixture_factory import write_ui_fixture


class ReviewDisplayCacheTests(unittest.TestCase):
    def test_catalog_summary_defers_source_hash_until_selected_verification(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = write_ui_fixture(Path(tmp))
            with patch.object(ui_data, "file_digest", wraps=file_digest) as digest:
                views = load_catalog(fixture["output"], Path(tmp) / "sources", verify_sources=False)
                self.assertTrue(views)
                self.assertTrue(all(view.source_available for view in views))
                self.assertTrue(all(not view.source_verified for view in views))
                digest.assert_not_called()

                selected = verify_selected_source(views[0])

            self.assertTrue(selected.source_available)
            self.assertTrue(selected.source_verified)
            self.assertEqual(digest.call_count, 1)

    def test_summary_keeps_existing_staged_source_available_without_verifying_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = write_ui_fixture(root)
            staged = root / "staged-copy.pdf"
            staged.write_bytes(b"staged scan")
            with patch.object(ui_data, "_source_for_display", return_value=""), \
                    patch.object(ui_data, "_batch_source_metadata", return_value={
                        fixture["digest"]: {"staged_path": str(staged)},
                    }):
                views = load_catalog(fixture["output"], root / "missing-source-root",
                                     verify_sources=False)

        staged_views = [view for view in views if view.source_hash == fixture["digest"]]
        self.assertTrue(staged_views)
        self.assertTrue(all(view.source_path == str(staged) for view in staged_views))
        self.assertTrue(all(view.source_available for view in staged_views))
        self.assertTrue(all(not view.source_verified for view in staged_views))

    def test_selected_source_verification_rejects_tampered_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "scan.jpg"
            source.write_bytes(b"original")
            candidate = RecipeCandidate(name="Toast", ingredients=["bread"], instructions=["toast"])
            view = ui_data.RecipeView(
                candidate_id="candidate", source_hash=file_digest(source), source_path=str(source),
                candidate=candidate, status="Approved", source_available=True,
            )
            source.write_bytes(b"tampered")

            selected = verify_selected_source(view)

        self.assertFalse(selected.source_available)
        self.assertFalse(selected.source_verified)

    def test_unchanged_cache_json_is_parsed_once_and_external_update_is_visible(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "abc.json"
            batch = ExtractionBatch(recipes=[RecipeCandidate(name="Toast", ingredients=["bread"], instructions=["toast"])])
            path.write_text(batch.model_dump_json(), encoding="utf-8")
            with patch.object(ui_data.json, "loads", wraps=json.loads) as loads:
                first = ui_data._load_caches(root)
                first_parse_count = loads.call_count
                second = ui_data._load_caches(root)
                self.assertEqual(loads.call_count, first_parse_count)
                self.assertEqual(first[0][1].recipes[0].name, "Toast")
                self.assertEqual(second[0][1].recipes[0].name, "Toast")

                changed = ExtractionBatch(recipes=[RecipeCandidate(name="Updated", ingredients=["bread"], instructions=["toast"])])
                path.write_text(changed.model_dump_json(), encoding="utf-8")
                stat = path.stat()
                path.touch()
                os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 10_000_000))
                third = ui_data._load_caches(root)

            self.assertEqual(third[0][1].recipes[0].name, "Updated")
            self.assertGreater(loads.call_count, first_parse_count)

    def test_cache_results_are_isolated_between_catalog_loads(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            batch = ExtractionBatch(recipes=[RecipeCandidate(name="Toast", ingredients=["bread"], instructions=["toast"])])
            (root / "abc.json").write_text(batch.model_dump_json(), encoding="utf-8")

            first = ui_data._load_caches(root)
            first[0][1].recipes[0].ingredients.append("mutation")
            second = ui_data._load_caches(root)

        self.assertEqual(second[0][1].recipes[0].ingredients, ["bread"])

    def test_source_digest_cache_uses_stat_key_and_invalidates_on_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "scan.jpg"
            path.write_bytes(b"first")
            digests = {}
            with patch.object(ui_data, "file_digest", wraps=file_digest) as digest:
                first = ui_data._cached_digest(path, digests)
                self.assertEqual(ui_data._cached_digest(path, digests), first)
                self.assertEqual(digest.call_count, 1)

                path.write_bytes(b"second")
                stat = path.stat()
                os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 10_000_000))
                digests.clear()
                second = ui_data._cached_digest(path, digests)

            self.assertNotEqual(first, second)
            self.assertEqual(digest.call_count, 2)

    def test_missing_source_fallback_indexes_tree_once_for_many_candidates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            expected = []
            for index in range(100):
                path = root / f"folder-{index}" / f"scan-{index}.jpg"
                path.parent.mkdir()
                path.write_bytes(b"scan")
                expected.append(path)
            source_index = _SourceIndex(root)
            original_rglob = Path.rglob
            calls = 0

            def track_rglob(path, pattern):
                nonlocal calls
                calls += 1
                return original_rglob(path, pattern)

            with patch.object(Path, "rglob", track_rglob):
                found = [
                    _source_for(str(index), None, {}, root, source_hint=f"old/scan-{index}.jpg",
                                source_index=source_index)
                    for index in range(100)
                ]

        self.assertEqual(found, [str(path) for path in expected])
        self.assertEqual(calls, 1)

    def test_display_fallback_does_not_guess_between_duplicate_basenames(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "one" / "scan.jpg"
            second = root / "two" / "scan.jpg"
            first.parent.mkdir()
            second.parent.mkdir()
            first.write_bytes(b"one")
            second.write_bytes(b"two")
            index = _SourceIndex(root)

            ambiguous = _source_for_display("candidate", None, {}, root, "old/scan.jpg", index)
            exact = _source_for_display("candidate", None, {}, root, "one/scan.jpg", index)

        self.assertEqual(ambiguous, "")
        self.assertEqual(exact, str(first))


if __name__ == "__main__":
    unittest.main()
