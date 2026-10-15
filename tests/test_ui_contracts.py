import unittest

from ui_contracts import normalize_status


class UiContractTests(unittest.TestCase):
    def test_imported_and_uncertain_records_are_protected(self):
        self.assertEqual(
            normalize_status("IMPORTED", candidate_complete=True, requires_review=False,
                             has_pending_suggestions=False, has_critical_findings=False),
            "Imported",
        )
        self.assertEqual(
            normalize_status("UNCERTAIN", candidate_complete=True, requires_review=False,
                             has_pending_suggestions=False, has_critical_findings=False),
            "Check Mealie",
        )

    def test_review_and_import_ready_states_are_distinct(self):
        self.assertEqual(
            normalize_status("", candidate_complete=True, requires_review=False,
                             has_pending_suggestions=False, has_critical_findings=False),
            "Ready to review",
        )
        self.assertEqual(
            normalize_status("", candidate_complete=True, requires_review=False,
                             has_pending_suggestions=False, has_critical_findings=False,
                             approved=True),
            "Ready to import",
        )
        self.assertEqual(
            normalize_status("", candidate_complete=True, requires_review=False,
                             has_pending_suggestions=True, has_critical_findings=False),
            "Needs attention",
        )

    def test_incomplete_and_failed_states_are_actionable(self):
        self.assertEqual(
            normalize_status("", candidate_complete=False, requires_review=False,
                             has_pending_suggestions=False, has_critical_findings=False),
            "Needs attention",
        )
        self.assertEqual(
            normalize_status("IMPORT_FAILED", candidate_complete=True, requires_review=False,
                             has_pending_suggestions=False, has_critical_findings=False),
            "Import failed",
        )


if __name__ == "__main__":
    unittest.main()
