from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from recipe_quality import RecipeCandidate, Suggestion
from review_store import ReviewConflict, ReviewStore, _candidate_payload, _canonical


class EffectiveIdentityApprovalTests(unittest.TestCase):
    def _legacy_pair(self, root: Path):
        source = root / "blend.jpg"
        source.write_bytes(b"synthetic Mediterranean blend scan")
        source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
        path = root / "review_events.jsonl"
        store = ReviewStore(path)
        original = RecipeCandidate(
            name="MEDITERRANEAN BLEND", ingredients=["1 tsp salt"],
            instructions=["Stir the ingredients together."],
        )
        prior = store.register_candidates(str(source), source_hash, "extract-v1", [original])[0]
        current = original.model_copy(update={"suggestions": [Suggestion(
            suggestion_id="step-detail", field_path="instructions[0]",
            proposed_value="Stir thoroughly.", rationale="Updated proposal.",
        )]})
        seeded = ReviewStore(root / "target-bindings.jsonl").register_candidates(
            str(source), source_hash, "extract-v2", [current]
        )[0]
        held_id = "held-mediterranean-blend-v2"
        held = store._base_event("candidate_registration", held_id, source_hash,
                                 "extract-v2", "legacy-held-registration")
        payload = _candidate_payload(current)
        held.update({
            "source": str(source),
            "fingerprint": hashlib.sha256(_canonical(payload).encode()).hexdigest(),
            "identity_status": "needs_reconciliation", "candidate": payload,
            "legacy_id": "", "title_legacy_id": "", "legacy_aliases": [],
            "target_bindings": seeded["target_bindings"],
        })
        with path.open("a", encoding="utf-8") as ledger:
            ledger.write(json.dumps(held) + "\n")
        return ReviewStore(path), source, source_hash, prior, held_id, current, seeded["target_bindings"]

    def test_confirmed_suggestion_only_match_can_approve_its_own_resolved_revision(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store, source, source_hash, prior, held_id, current, bindings = self._legacy_pair(root)
            self.assertEqual(store.registrations[held_id][0]["identity_status"], "needs_reconciliation")
            self.assertTrue(next(row for row in store.identity_reconciliation_candidates()
                                 if row["candidate_id"] == held_id)["eligible"])
            store.confirm_identity_match(held_id, prior["candidate_id"])
            self.assertEqual(store.identity_link_status(held_id, source_hash, "extract-v2"), "registered")

            issue = next(item for item in bindings
                         if item.get("issue_id") == "step-detail")
            revision = store.save_revision(
                candidate_id=held_id, source_hash=source_hash, extraction_id="extract-v2",
                expected_parent=0, operation_id="save-linked-revision",
                recipe=current.model_copy(update={
                    "instructions": ["Stir thoroughly."]
                }).model_dump(mode="json"),
                target_bindings=[{"target_id": issue["source_target_id"],
                                  "field": "instructions", "position": 0}],
            )
            store.resolve_issue(
                candidate_id=held_id, source_hash=source_hash, extraction_id="extract-v2",
                revision=1, issue_id="step-detail", target_id=issue["target_id"],
                disposition="accepted", final_value="Stir thoroughly.", operation_id="accept-v2-proposal",
            )

            approval = store.approve_revision(
                candidate_id=held_id, source_hash=source_hash, extraction_id="extract-v2",
                revision=revision["revision"], operation_id="approve-linked-v2", source_path=source,
            )
            reopened = ReviewStore(root / "review_events.jsonl")
            projection = reopened.project(held_id, source_hash, "extract-v2", current)
            approval_events = [item for item in reopened.approvals[held_id]
                               if item.get("extraction_id") == "extract-v2"]

        self.assertEqual(approval["event"], "review_approval")
        self.assertEqual(len(approval_events), 1)
        self.assertTrue(projection.import_ready)

    def test_link_for_different_prior_candidate_does_not_resolve_registration(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store, _source, source_hash, _prior, held_id, _current, _bindings = self._legacy_pair(root)
            link = store._base_event("candidate_identity_link", held_id, source_hash,
                                     "extract-v2", "malformed-link")
            link.update({"previous_candidate_id": "not-registered", "identity_group_id": "other",
                         "logical_identity_digest": "not-the-recipe-digest", "comparison_version": 1,
                         "decision": "confirmed_unique_content_match"})
            with store.path.open("a", encoding="utf-8") as ledger:
                ledger.write(json.dumps(link) + "\n")

            status = ReviewStore(store.path).identity_link_status(held_id, source_hash, "extract-v2")

        self.assertEqual(status, "needs_reconciliation")

    def test_link_for_another_extraction_does_not_resolve_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store, _source, source_hash, prior, held_id, current, _bindings = self._legacy_pair(root)
            link = store._base_event("candidate_identity_link", held_id, source_hash,
                                     "extract-not-v2", "wrong-extraction-link")
            link.update({"previous_candidate_id": prior["candidate_id"],
                         "previous_extraction_id": "extract-v1",
                         "identity_group_id": prior["candidate_id"],
                         "logical_identity_digest": prior["logical_identity_digest"],
                         "comparison_version": 1, "decision": "confirmed_unique_content_match"})
            with store.path.open("a", encoding="utf-8") as ledger:
                ledger.write(json.dumps(link) + "\n")

            status = ReviewStore(store.path).identity_link_status(held_id, source_hash, "extract-v2")

        self.assertEqual(status, "needs_reconciliation")

    def test_conflicting_links_do_not_authorize_held_registration(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store, _source, source_hash, prior, held_id, _current, _bindings = self._legacy_pair(root)
            for suffix in ("first", "second"):
                link = store._base_event("candidate_identity_link", held_id, source_hash,
                                         "extract-v2", f"conflicting-{suffix}")
                link.update({"previous_candidate_id": prior["candidate_id"],
                             "previous_extraction_id": "extract-v1",
                             "identity_group_id": prior["candidate_id"],
                             "logical_identity_digest": prior["logical_identity_digest"],
                             "comparison_version": 1, "decision": "confirmed_unique_content_match"})
                with store.path.open("a", encoding="utf-8") as ledger:
                    ledger.write(json.dumps(link) + "\n")

            status = ReviewStore(store.path).identity_link_status(held_id, source_hash, "extract-v2")

        self.assertEqual(status, "needs_reconciliation")

    def test_linked_candidate_can_create_a_scoped_suggestion_request_without_provider_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store, source, source_hash, prior, held_id, current, _bindings = self._legacy_pair(root)
            store.confirm_identity_match(held_id, prior["candidate_id"])
            revision = store.save_revision(
                candidate_id=held_id, source_hash=source_hash, extraction_id="extract-v2",
                expected_parent=0, operation_id="request-revision",
                recipe=current.model_dump(mode="json"), target_bindings=[],
            )
            request = store.create_suggestion_request(
                candidate_id=held_id, source_hash=source_hash, extraction_id="extract-v2",
                revision=revision["revision"], content_digest=revision["content_sha256"],
                recipe_snapshot={"name": current.name, "ingredients": current.ingredients,
                                 "instructions": current.instructions},
                suggestion_type="instructions", operation_id="linked-suggestion-request",
                consent=True,
            )
            reopened = ReviewStore(store.path)

        self.assertEqual(request["status"], "queued")
        self.assertEqual(request["candidate_id"], held_id)
        self.assertEqual(request["extraction_id"], "extract-v2")
        self.assertEqual(reopened.latest_suggestion_request_loaded(
            held_id, source_hash, "extract-v2")["request_id"], request["request_id"])

    def test_unlinked_candidate_stays_blocked_from_suggestion_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store, _source, source_hash, _prior, held_id, current, _bindings = self._legacy_pair(root)
            revision = store.save_revision(
                candidate_id=held_id, source_hash=source_hash, extraction_id="extract-v2",
                expected_parent=0, operation_id="unlinked-request-revision",
                recipe=current.model_dump(mode="json"), target_bindings=[],
            )
            with self.assertRaisesRegex(ReviewConflict, "identity is not ready"):
                store.create_suggestion_request(
                    candidate_id=held_id, source_hash=source_hash, extraction_id="extract-v2",
                    revision=revision["revision"], content_digest=revision["content_sha256"],
                    recipe_snapshot={"name": current.name, "ingredients": current.ingredients,
                                     "instructions": current.instructions},
                    suggestion_type="instructions", operation_id="unlinked-suggestion-request",
                    consent=True,
                )

    def test_linked_candidate_can_complete_a_valid_stale_suggestion_rebase(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store, source, source_hash, prior, held_id, current, _bindings = self._legacy_pair(root)
            store.confirm_identity_match(held_id, prior["candidate_id"])
            targets = [{"target_id": "current-step", "target_kind": "row",
                        "field": "instructions", "position": 0}]
            revision = store.save_revision(
                candidate_id=held_id, source_hash=source_hash, extraction_id="extract-v2",
                expected_parent=0, operation_id="rebase-base-revision",
                recipe=current.model_dump(mode="json"), target_bindings=targets,
            )
            request = store.create_suggestion_request(
                candidate_id=held_id, source_hash=source_hash, extraction_id="extract-v2",
                revision=revision["revision"], content_digest=revision["content_sha256"],
                recipe_snapshot={"name": current.name, "ingredients": current.ingredients,
                                 "instructions": current.instructions},
                suggestion_type="instructions", operation_id="rebase-request", consent=True,
                target_bindings=[{"target_id": "current-step", "field": "instructions"}],
            )
            store.claim_suggestion_request(request["request_id"], "synthetic-worker")
            intervening = current.model_copy(update={"instructions": ["Stir very gently."]})
            latest = store.save_revision(
                candidate_id=held_id, source_hash=source_hash, extraction_id="extract-v2",
                expected_parent=1, operation_id="rebase-intervening-revision",
                recipe=intervening.model_dump(mode="json"), target_bindings=targets,
            )
            proposal = store.record_dynamic_proposals(request["request_id"], [{
                "field_path": "instructions[0]", "target_id": "current-step",
                "proposed_value": "Stir vigorously.", "classification": "inferred",
                "rationale": "Synthetic review proposal.", "uncertainty": "Not verified by source.",
            }])[0]
            projected = RecipeCandidate.model_validate(
                store.project(held_id, source_hash, "extract-v2", intervening).recipe)
            projected.instructions[0] = "Stir vigorously."

            saved, decision = store.rebase_and_apply_dynamic_suggestion(
                candidate_id=held_id, proposal_id=proposal["proposal_id"],
                expected_parent=latest["revision"], expected_content_digest=latest["content_sha256"],
                recipe=projected.model_dump(mode="json"),
                target_bindings=latest["target_bindings"], disposition="accepted",
                final_value="Stir vigorously.", source_path=source, confirmed=True,
                operation_id="linked-rebase",
            )

        self.assertEqual(saved["revision"], 3)
        self.assertEqual(decision["candidate_id"], held_id)
        self.assertEqual(decision["extraction_id"], "extract-v2")

    def test_linked_candidate_with_import_protection_cannot_receive_new_approval(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store, source, source_hash, prior, held_id, current, bindings = self._legacy_pair(root)
            store.confirm_identity_match(held_id, prior["candidate_id"])
            issue = next(item for item in bindings if item.get("issue_id") == "step-detail")
            resolved_recipe = current.model_copy(update={"instructions": ["Stir thoroughly."]})
            revision = store.save_revision(
                candidate_id=held_id, source_hash=source_hash, extraction_id="extract-v2",
                expected_parent=0, operation_id="protected-linked-revision",
                recipe=resolved_recipe.model_dump(mode="json"),
                target_bindings=[{"target_id": issue["source_target_id"],
                                  "field": "instructions", "position": 0}],
            )
            store.resolve_issue(
                candidate_id=held_id, source_hash=source_hash, extraction_id="extract-v2",
                revision=revision["revision"], issue_id="step-detail", target_id=issue["target_id"],
                disposition="accepted", final_value="Stir thoroughly.", operation_id="protect-accepted-proposal",
            )
            (root / "import_state.jsonl").write_text(json.dumps({
                "identity": held_id, "status": "IMPORTED",
            }) + "\n", encoding="utf-8")

            with self.assertRaisesRegex(ReviewConflict, "protected import outcome"):
                store.approve_revision(
                    candidate_id=held_id, source_hash=source_hash, extraction_id="extract-v2",
                    revision=revision["revision"], operation_id="protected-linked-approval",
                    source_path=source,
                )

    def test_previous_extraction_approval_does_not_transfer_to_linked_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store, source, source_hash, prior, held_id, current, _bindings = self._legacy_pair(root)
            previous_recipe = RecipeCandidate(
                name="MEDITERRANEAN BLEND", ingredients=["1 tsp salt"],
                instructions=["Stir the ingredients together."],
            )
            previous_revision = store.save_revision(
                candidate_id=prior["candidate_id"], source_hash=source_hash, extraction_id="extract-v1",
                expected_parent=0, operation_id="previous-extraction-revision",
                recipe=previous_recipe.model_dump(mode="json"), target_bindings=prior["target_bindings"],
            )
            store.approve_revision(
                candidate_id=prior["candidate_id"], source_hash=source_hash, extraction_id="extract-v1",
                revision=previous_revision["revision"], operation_id="previous-extraction-approval",
                source_path=source,
            )
            store.confirm_identity_match(held_id, prior["candidate_id"])
            current_revision = store.save_revision(
                candidate_id=held_id, source_hash=source_hash, extraction_id="extract-v2",
                expected_parent=0, operation_id="new-extraction-revision",
                recipe=current.model_dump(mode="json"), target_bindings=[],
            )
            previous_projection = store.project(prior["candidate_id"], source_hash, "extract-v1", previous_recipe)
            current_projection = ReviewStore(store.path).project(held_id, source_hash, "extract-v2", current)

        self.assertTrue(previous_projection.import_ready)
        self.assertEqual(current_revision["revision"], 1)
        self.assertFalse(current_projection.approved)
        self.assertFalse(current_projection.import_ready)

    def test_linked_candidate_still_requires_resolution_of_its_pending_suggestion(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store, source, source_hash, prior, held_id, current, bindings = self._legacy_pair(root)
            store.confirm_identity_match(held_id, prior["candidate_id"])
            issue = next(item for item in bindings if item.get("issue_id") == "step-detail")
            revision = store.save_revision(
                candidate_id=held_id, source_hash=source_hash, extraction_id="extract-v2",
                expected_parent=0, operation_id="pending-linked-revision",
                recipe=current.model_dump(mode="json"),
                target_bindings=[{"target_id": issue["source_target_id"],
                                  "field": "instructions", "position": 0}],
            )

            with self.assertRaisesRegex(ReviewConflict, "pending proposals must be resolved"):
                store.approve_revision(
                    candidate_id=held_id, source_hash=source_hash, extraction_id="extract-v2",
                    revision=revision["revision"], operation_id="pending-linked-approval", source_path=source,
                )


if __name__ == "__main__":
    unittest.main()
