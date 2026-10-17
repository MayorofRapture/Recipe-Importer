import unittest
from types import SimpleNamespace

from suggestion_ui import (
    _display_field_path,
    _provenance_label,
    approval_blockers,
    suggestion_current_value,
    suggestion_diff,
    suggestion_summary,
    suggestion_status_label,
)


class SuggestionUiProjectionTests(unittest.TestCase):
    def test_field_path_headers_use_reader_facing_labels(self):
        self.assertEqual(_display_field_path("instructions[0]"), "Step 1")
        self.assertEqual(_display_field_path("ingredients[2]"), "Ingredient 3")
        self.assertEqual(_display_field_path("yield_text"), "Yield")

    def test_inferred_provenance_distinguishes_human_decisions_from_source_evidence(self):
        self.assertEqual(_provenance_label("inferred", "pending"),
                         "AI-inferred completion · unverified")
        self.assertEqual(_provenance_label("inferred", "accepted"),
                         "AI-inferred completion · human-approved")
        self.assertEqual(_provenance_label("inferred", "edited"),
                         "AI-inferred completion · human-edited")
        self.assertEqual(_provenance_label("source_supported", "approved"),
                         "Source-supported proposal")

    def test_summary_counts_all_saved_decision_states(self):
        suggestions = [
            SimpleNamespace(review_status="pending"),
            SimpleNamespace(review_status="approved"),
            SimpleNamespace(review_status="edited"),
            SimpleNamespace(review_status="rejected"),
            SimpleNamespace(review_status="stale"),
            SimpleNamespace(review_status="target_missing"),
        ]
        self.assertEqual(
            suggestion_summary(suggestions),
            "Suggestions: 1 awaiting review " + chr(0x00b7) + " 1 applied " + chr(0x00b7)
            + " 1 applied with edits " + chr(0x00b7) + " 1 rejected; source issue unresolved "
            + chr(0x00b7) + " 1 stale " + chr(0x00b7) + " 1 could not apply",
        )

    def test_current_wording_uses_registered_target_identity(self):
        candidate = SimpleNamespace(instructions=["Stir", "Bake"])
        suggestion = SimpleNamespace(suggestion_id="proposal", target_id=None)
        bindings = [
            {"issue_id": "proposal", "source_target_id": "step-b"},
            {"target_id": "step-a", "target_kind": "row", "field": "instructions", "position": 0},
            {"target_id": "step-b", "target_kind": "row", "field": "instructions", "position": 1},
        ]
        self.assertEqual(suggestion_current_value(suggestion, candidate, bindings), "Bake")
        self.assertEqual(suggestion_diff("Bake", "Bake until set."), "- Bake\n+ Bake until set.")

    def test_generated_proposal_current_wording_uses_revision_target_id(self):
        candidate = SimpleNamespace(instructions=["Cook rice.", "Fold in egg."])
        suggestion = SimpleNamespace(suggestion_id="dynamic-proposal", target_id="revision-step-1")
        revision_bindings = [
            {"target_id": "revision-step-0", "field": "instructions", "position": 0},
            {"target_id": "revision-step-1", "field": "instructions", "position": 1},
        ]
        self.assertEqual(suggestion_current_value(suggestion, candidate, revision_bindings), "Fold in egg.")

    def test_dynamic_status_overrides_pending_projection_after_recipe_changes(self):
        suggestion = SimpleNamespace(review_status="pending", dynamic_status="stale")
        view = SimpleNamespace(
            protected=False, identity_status="registered", source_available=True,
            legacy_draft_conflict=False, critical_findings=[],
            candidate=SimpleNamespace(name="Soup", ingredients=["1 cup rice"], instructions=["Cook"],
                                      suggestions=[suggestion]),
        )
        self.assertEqual(suggestion_summary([suggestion]), "Suggestions: 1 stale")
        self.assertEqual(approval_blockers(view)[0].label,
                         "1 suggestion need comparison with the current recipe")

    def test_status_labels_distinguish_declined_from_unresolved_rejection(self):
        self.assertEqual(suggestion_status_label("declined"), "Declined " + chr(0x2014) + " no change required")
        self.assertEqual(suggestion_status_label("rejected"), "Rejected " + chr(0x2014) + " source issue still unresolved")
        self.assertEqual(suggestion_status_label("pending"), "Awaiting review")

    def test_blockers_explain_critical_and_pending_suggestion_with_anchor_keys(self):
        view = SimpleNamespace(
            protected=False, identity_status="registered", source_available=True,
            legacy_draft_conflict=False, critical_findings=[object()],
            pending_suggestions=[object()], candidate=SimpleNamespace(
                name="Soup", ingredients=["1 cup rice"], instructions=["Cook"],
                suggestions=[SimpleNamespace(review_status="pending")],
            ),
        )
        blockers = approval_blockers(view)
        self.assertEqual([item.label for item in blockers], [
            "1 critical source issue unresolved", "1 suggestion awaiting review",
        ])
        self.assertEqual([item.anchor for item in blockers], ["critical-findings", "suggestions"])

    def test_incomplete_recipe_blocker_names_missing_instructions(self):
        view = SimpleNamespace(
            protected=False, identity_status="registered", source_available=True,
            legacy_draft_conflict=False, critical_findings=[], pending_suggestions=[],
            candidate=SimpleNamespace(name="Soup", ingredients=["1 cup rice"], instructions=[]),
        )
        self.assertEqual(approval_blockers(view)[0].label, "Missing recipe instructions")


if __name__ == "__main__":
    unittest.main()
