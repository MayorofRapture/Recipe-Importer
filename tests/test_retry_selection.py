import unittest
from pathlib import Path
from unittest.mock import patch

from import_recipes import StateStore, retry_notice, select_sources


class RetrySelectionTests(unittest.TestCase):
    def test_limit_is_applied_after_incomplete_source_filter(self):
        complete = Path("001-complete.jpg")
        incomplete = Path("002-incomplete.jpg")
        with patch("import_recipes._source_has_retryable_cache",
                   side_effect=lambda state, path, digest, cache: digest == "incomplete-hash"):
            selected = select_sources(
                [(complete, "complete-hash"), (incomplete, "incomplete-hash")],
                StateStore(Path("unused-state.jsonl")), Path("unused-cache"),
                Path("unused-report.csv"), retry_incomplete=True, limit=1,
            )

        self.assertEqual(selected, [(incomplete, "incomplete-hash")])

    def test_mixed_source_is_selected_without_filtering_imported_recipe(self):
        source = Path("mixed.jpg")
        with patch("import_recipes._source_has_retryable_cache", return_value=True):
            selected = select_sources(
                [(source, "mixed-hash")], StateStore(Path("unused-state.jsonl")),
                Path("unused-cache"), Path("unused-report.csv"),
                retry_incomplete=True, limit=1,
            )

        self.assertEqual(selected, [(source, "mixed-hash")])

    def test_failed_report_makes_source_eligible_without_cache(self):
        failed = Path("failed.jpg")
        with patch("import_recipes._load_retry_report", return_value=[{
            "file": str(failed), "status": "EXTRACTION_FAILED", "model": "primary",
        }]):
            selected = select_sources(
                [(failed, "failed-hash")], StateStore(Path("unused-state.jsonl")),
                Path("unused-cache"), Path("unused-report.csv"),
                retry_incomplete=True, limit=1,
            )

        self.assertEqual(selected, [(failed, "failed-hash")])

    def test_retry_notice_includes_file_and_model(self):
        notice = retry_notice(Path("recipes/second.jpg"), ["primary", "quality"])

        self.assertIn("recipes\\second.jpg", notice)
        self.assertIn("model=primary", notice)


if __name__ == "__main__":
    unittest.main()
