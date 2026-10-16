"""R1.2 review authority, immutable targets, idempotency, and process locks."""
from __future__ import annotations

import json
import hashlib
import csv
import subprocess
import sys
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from recipe_quality import Diagnostic, RecipeCandidate, Suggestion
from review_store import ReviewConflict, ReviewStore

SOURCE_BYTES = b"synthetic recipe scan for review authority tests"
SOURCE_HASH = hashlib.sha256(SOURCE_BYTES).hexdigest()


def _source_path(directory):
    path = Path(directory) / "source.jpg"
    path.write_bytes(SOURCE_BYTES)
    return path
from draft_store import DraftRecord


_SAME_PARENT_WORKER = """
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd()))
from review_store import ReviewConflict, ReviewStore
path, candidate_id, operation_id = sys.argv[1:]
store = ReviewStore(Path(path))
print('READY', flush=True)
sys.stdin.readline()
try:
    event = store.save_revision(
        candidate_id=candidate_id, source_hash='source-hash', extraction_id='extract-1',
        expected_parent=0, operation_id=operation_id,
        recipe={'name': 'Rice', 'ingredients': ['1 cup rice'], 'instructions': ['Cook.']},
        target_bindings=[],
    )
    print(json.dumps(['saved', event['revision']]), flush=True)
except ReviewConflict:
    print(json.dumps(['conflict', 0]), flush=True)
"""


class ReviewAuthorityTests(unittest.TestCase):
    def test_registration_snapshot_refreshes_when_another_writer_registered_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "review_events.jsonl"
            stale = ReviewStore(path)
            candidate = RecipeCandidate(
                name="Rice", ingredients=["rice"], instructions=["Cook."],
            )
            external = ReviewStore(path).register_candidates(
                "scan", SOURCE_HASH, "extract-1", [candidate],
            )[0]

            observed = stale.register_candidates(
                "scan", SOURCE_HASH, "extract-1", [candidate],
            )[0]
            events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

        self.assertEqual(observed["candidate_id"], external["candidate_id"])
        self.assertEqual(sum(item.get("event") == "candidate_registration" for item in events), 1)

    def test_csv_projects_modern_issue_resolutions_and_keeps_each_original_proposal(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            source = output / "scan.jpg"
            source.write_bytes(SOURCE_BYTES)
            candidate = RecipeCandidate(
                name="Date Bars", ingredients=["dates"], instructions=["Mix."],
                missing_information=[Diagnostic(
                    field_path="ingredients[0]", kind="ambiguous", severity="critical",
                    detail="The quantity is unclear.", source_evidence="The left edge is cropped.",
                    suggested_action="Compare the scan.",
                )],
                suggestions=[
                    Suggestion(suggestion_id="quantity-a", field_path="ingredients[0]",
                               proposed_value="1 cup dates", rationale="First reading."),
                    Suggestion(suggestion_id="quantity-b", field_path="ingredients[0]",
                               proposed_value="1/2 cup dates", rationale="Second reading."),
                ],
            )
            store = ReviewStore(output / "review_events.jsonl")
            registration = store.register_candidates(
                str(source), SOURCE_HASH, "extraction-1", [candidate],
            )[0]
            for issue_id, disposition, value in (
                ("quantity-a", "accepted", "1 cup dates"),
                ("quantity-b", "edited", "3/4 cup dates"),
            ):
                binding = next(item for item in registration["target_bindings"]
                               if item.get("issue_id") == issue_id)
                store.resolve_issue(
                    candidate_id=registration["candidate_id"], source_hash=SOURCE_HASH,
                    extraction_id="extraction-1", revision=0, issue_id=issue_id,
                    target_id=binding["target_id"], disposition=disposition,
                    final_value=value, operation_id=f"resolution-{issue_id}",
                )
            report = output / "needs_review.csv"
            store.write_csv(report)
            with report.open(encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))

        proposals = {row["suggestion_id"]: row for row in rows}
        self.assertEqual(set(proposals), {"quantity-a", "quantity-b"})
        self.assertEqual(proposals["quantity-a"]["review_status"], "approved")
        self.assertEqual(proposals["quantity-a"]["suggested_completion"], "1 cup dates")
        self.assertEqual(proposals["quantity-a"]["approved_value"], "1 cup dates")
        self.assertEqual(proposals["quantity-b"]["review_status"], "edited")
        self.assertEqual(proposals["quantity-b"]["suggested_completion"], "1/2 cup dates")
        self.assertEqual(proposals["quantity-b"]["approved_value"], "3/4 cup dates")

    def test_candidate_registration_is_opaque_repeatable_and_holds_identical_siblings(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ReviewStore(Path(tmp) / "review_events.jsonl")
            first = RecipeCandidate(name="Rice", ingredients=["1 cup rice"], instructions=["Cook."])
            second = RecipeCandidate(name="Rice", ingredients=["2 cups rice"], instructions=["Steam."])
            registrations = store.register_candidates(
                "scan.pdf", "source-hash", "extract-1", [first, second]
            )
            again = ReviewStore(store.path).register_candidates(
                "scan.pdf", "source-hash", "extract-1", [second, first]
            )
            identical = store.register_candidates(
                "scan.pdf", "source-hash", "extract-2", [first, first]
            )
            identical_again = ReviewStore(store.path).register_candidates(
                "scan.pdf", "source-hash", "extract-2", [first, first]
            )

        self.assertNotEqual(registrations[0]["candidate_id"], registrations[1]["candidate_id"])
        self.assertTrue(all(str(uuid.UUID(item["candidate_id"])) == item["candidate_id"]
                            for item in registrations))
        self.assertEqual({item["candidate_id"] for item in registrations},
                         {item["candidate_id"] for item in again})
        self.assertEqual([item["identity_status"] for item in identical],
                         ["needs_reconciliation", "needs_reconciliation"])
        self.assertEqual([item["candidate_id"] for item in identical],
                         [item["candidate_id"] for item in identical_again])

    def test_unmatched_candidate_is_held_when_legacy_import_history_exists_for_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_hash = "legacy-source-hash"
            state_path = root / "import_state.jsonl"
            state_path.write_text(json.dumps({
                "key": f"{source_hash}:0", "status": "IMPORTED", "title": "Old title",
            }) + "\n", encoding="utf-8")
            store = ReviewStore(root / "review_events.jsonl", state_path=state_path)
            renamed = RecipeCandidate(
                name="Renamed title", ingredients=["1 cup beans"], instructions=["Simmer."],
            )

            registration = store.register_candidates(
                "scan.jpg", source_hash, "fresh-extraction", [renamed],
            )[0]

        self.assertEqual(registration["identity_status"], "needs_reconciliation")

    def test_malformed_event_tail_fails_closed_without_rewriting_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "review_events.jsonl"
            damaged = b'{"event":"candidate_registration"}\n{"event":"review_revision"'
            path.write_bytes(damaged)

            with self.assertRaises(json.JSONDecodeError):
                ReviewStore(path)

            self.assertEqual(path.read_bytes(), damaged)

    def test_unique_legacy_candidate_id_is_preserved_during_registration(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ReviewStore(Path(tmp) / "review_events.jsonl")
            candidate = RecipeCandidate(name="Rice", ingredients=["1 cup rice"], instructions=["Cook."])
            store.add_candidate("scan.pdf", "legacy-rice-id", candidate)
            registered = store.register_candidates("scan.pdf", "source-hash", "extract-1", [candidate])[0]

        self.assertEqual(registered["candidate_id"], "legacy-rice-id")

    def test_issue_resolution_retry_is_idempotent_and_tracks_reordered_source_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ReviewStore(Path(tmp) / "review_events.jsonl")
            candidate = RecipeCandidate(
                name="Bars", ingredients=["dates", "cranberries"], instructions=["Mix."],
                suggestions=[Suggestion(
                    suggestion_id="quantity", field_path="ingredients[0]",
                    proposed_value="1 cup dates", rationale="possible reading",
                    confidence="low", uncertainty="cropped",
                )],
            )
            registration = store.register_candidates("scan.pdf", SOURCE_HASH, "extract-1", [candidate])[0]
            issue = next(item for item in registration["target_bindings"]
                         if item.get("issue_id") == "quantity")
            source_row_id = issue["source_target_id"]
            store.save_revision(
                candidate_id=registration["candidate_id"], source_hash=SOURCE_HASH,
                extraction_id="extract-1", expected_parent=0, operation_id="save-1",
                recipe={"name": "Bars", "ingredients": ["dates", "cranberries"],
                        "instructions": ["Mix."]},
                target_bindings=[{"target_id": source_row_id, "field": "ingredients", "position": 0}],
            )
            store.save_revision(
                candidate_id=registration["candidate_id"], source_hash=SOURCE_HASH,
                extraction_id="extract-1", expected_parent=1, operation_id="save-2",
                recipe={"name": "Bars", "ingredients": ["cranberries", "dates"],
                        "instructions": ["Mix."], "suggestions": candidate.model_dump(mode="json")["suggestions"]},
                target_bindings=[{"target_id": source_row_id, "field": "ingredients", "position": 1}],
            )
            first = store.resolve_issue(
                candidate_id=registration["candidate_id"], source_hash=SOURCE_HASH,
                extraction_id="extract-1", revision=2, issue_id="quantity",
                target_id=issue["target_id"], disposition="edited", final_value="3/4 cup dates",
                operation_id="resolve-once",
            )
            retried = ReviewStore(store.path).resolve_issue(
                candidate_id=registration["candidate_id"], source_hash=SOURCE_HASH,
                extraction_id="extract-1", revision=2, issue_id="quantity",
                target_id=issue["target_id"], disposition="edited", final_value="3/4 cup dates",
                operation_id="resolve-once",
            )
            projection = store.project(registration["candidate_id"], SOURCE_HASH, "extract-1", candidate)

        self.assertEqual(first["event_id"], retried["event_id"])
        self.assertEqual(projection.recipe["ingredients"][1], "3/4 cup dates")

    def test_approval_binds_the_exact_resolution_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ReviewStore(Path(tmp) / "review_events.jsonl")
            candidate = RecipeCandidate(
                name="Bars", ingredients=["dates"], instructions=["Mix."],
                suggestions=[Suggestion(
                    suggestion_id="quantity", field_path="ingredients[0]",
                    proposed_value="1 cup dates", rationale="possible reading",
                    confidence="low", uncertainty="cropped",
                )],
            )
            registration = store.register_candidates("scan", SOURCE_HASH, "extract-1", [candidate])[0]
            issue = next(item for item in registration["target_bindings"]
                         if item.get("issue_id") == "quantity")
            store.save_revision(
                candidate_id=registration["candidate_id"], source_hash=SOURCE_HASH, extraction_id="extract-1",
                expected_parent=0, operation_id="resolution-payload-revision",
                recipe=candidate.model_dump(mode="json"),
                target_bindings=[{"target_id": issue["source_target_id"],
                                  "field": "ingredients", "position": 0}],
            )
            store.resolve_issue(
                candidate_id=registration["candidate_id"], source_hash=SOURCE_HASH,
                extraction_id="extract-1", revision=1, issue_id="quantity",
                target_id=issue["target_id"], disposition="edited", final_value="3/4 cup dates",
                operation_id="resolution-payload-decision",
            )
            store.approve_revision(
                candidate_id=registration["candidate_id"], source_hash=SOURCE_HASH,
                extraction_id="extract-1", revision=1, operation_id="resolution-payload-approval",
                source_path=_source_path(tmp),
            )
            approved = store.project(registration["candidate_id"], SOURCE_HASH, "extract-1", candidate)
            lines = [json.loads(line) for line in store.path.read_text(encoding="utf-8").splitlines()]
            for item in lines:
                if item.get("event") == "review_issue_resolution":
                    item["final_value"] = "2 cups dates"
            store.path.write_text("".join(json.dumps(item) + "\n" for item in lines), encoding="utf-8")
            tampered = ReviewStore(store.path).project(
                registration["candidate_id"], SOURCE_HASH, "extract-1", candidate)

        self.assertTrue(approved.import_ready)
        self.assertEqual(approved.recipe["ingredients"], ["3/4 cup dates"])
        self.assertFalse(tampered.import_ready)

    def test_issue_resolution_binds_to_immutable_target_not_current_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ReviewStore(Path(tmp) / "review_events.jsonl")
            candidate = RecipeCandidate(
                name="Bars", ingredients=["dates", "cranberries"], instructions=["Mix."],
                suggestions=[Suggestion(
                    suggestion_id="quantity", field_path="ingredients[0]",
                    proposed_value="1 cup dates", rationale="possible reading",
                    confidence="low", uncertainty="cropped",
                )],
            )
            registration = store.register_candidates("scan.pdf", SOURCE_HASH, "extract-1", [candidate])[0]
            binding = next(item for item in registration["target_bindings"]
                           if item.get("issue_id") == "quantity")
            revision = store.save_revision(
                candidate_id=registration["candidate_id"], source_hash=SOURCE_HASH,
                extraction_id="extract-1", expected_parent=0, operation_id="save-before-resolution",
                recipe={"name": "Bars", "ingredients": ["cranberries", "dates"],
                        "instructions": ["Mix."]},
                target_bindings=[{"target_id": binding["source_target_id"], "field": "ingredients", "position": 1}],
            )
            store.resolve_issue(
                candidate_id=registration["candidate_id"], source_hash=SOURCE_HASH,
                extraction_id="extract-1", revision=1, issue_id=binding["issue_id"],
                target_id=binding["target_id"], disposition="edited", final_value="3/4 cup dates",
                operation_id="resolve-once",
            )
            projection = store.project(
                registration["candidate_id"], SOURCE_HASH, "extract-1", candidate
            )

        self.assertEqual(revision["revision"], 1)
        self.assertEqual(projection.recipe["ingredients"][1], "3/4 cup dates")
        self.assertEqual(projection.resolutions[0]["target_id"], binding["target_id"])

    def test_whole_field_instruction_proposal_applies_to_registered_field_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ReviewStore(Path(tmp) / "review_events.jsonl")
            candidate = RecipeCandidate(
                name="Soup", ingredients=["water"], instructions=["Cook."],
                suggestions=[Suggestion(
                    suggestion_id="directions", field_path="instructions",
                    proposed_value="Simmer for 20 minutes.", rationale="A possible omitted direction.",
                )],
            )
            registration = store.register_candidates("scan", SOURCE_HASH, "extract-1", [candidate])[0]
            issue = next(item for item in registration["target_bindings"]
                         if item.get("issue_id") == "directions")
            self.assertTrue(issue["source_target_id"])
            store.save_revision(
                candidate_id=registration["candidate_id"], source_hash=SOURCE_HASH,
                extraction_id="extract-1", expected_parent=0, operation_id="whole-field-revision",
                recipe=candidate.model_dump(mode="json"), target_bindings=[],
            )
            store.resolve_issue(
                candidate_id=registration["candidate_id"], source_hash=SOURCE_HASH,
                extraction_id="extract-1", revision=1, issue_id="directions",
                target_id=issue["target_id"], disposition="accepted",
                final_value="Simmer for 20 minutes.", operation_id="whole-field-resolution",
            )
            projection = store.project(registration["candidate_id"], SOURCE_HASH, "extract-1", candidate)

        self.assertEqual(projection.recipe["instructions"], ["Simmer for 20 minutes."])
        self.assertEqual(projection.recipe["suggestions"][0]["review_status"], "approved")

    def test_scalar_yield_proposal_applies_to_immutable_scalar_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ReviewStore(Path(tmp) / "review_events.jsonl")
            candidate = RecipeCandidate(
                name="Soup", ingredients=["water"], instructions=["Cook."],
                suggestions=[Suggestion(
                    suggestion_id="yield", field_path="yield_text", proposed_value="4 servings",
                    rationale="A possible yield.",
                )],
            )
            registration = store.register_candidates("scan", SOURCE_HASH, "extract-1", [candidate])[0]
            issue = next(item for item in registration["target_bindings"]
                         if item.get("issue_id") == "yield")
            self.assertTrue(issue["source_target_id"])
            store.save_revision(
                candidate_id=registration["candidate_id"], source_hash=SOURCE_HASH,
                extraction_id="extract-1", expected_parent=0, operation_id="scalar-field-revision",
                recipe=candidate.model_dump(mode="json"), target_bindings=[],
            )
            store.resolve_issue(
                candidate_id=registration["candidate_id"], source_hash=SOURCE_HASH,
                extraction_id="extract-1", revision=1, issue_id="yield",
                target_id=issue["target_id"], disposition="edited", final_value="6 servings",
                operation_id="scalar-field-resolution",
            )
            projection = store.project(registration["candidate_id"], SOURCE_HASH, "extract-1", candidate)

        self.assertEqual(projection.recipe["yield_text"], "6 servings")
        self.assertEqual(projection.recipe["suggestions"][0]["review_status"], "edited")

    def test_rejected_optional_proposal_remains_blocking_and_is_never_applied(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ReviewStore(Path(tmp) / "review_events.jsonl")
            candidate = RecipeCandidate(
                name="Soup", ingredients=["water"], instructions=["Cook."],
                suggestions=[Suggestion(
                    suggestion_id="yield", field_path="yield_text", proposed_value="4 servings",
                    rationale="An unverified guess.",
                )],
            )
            registration = store.register_candidates("scan", SOURCE_HASH, "extract-1", [candidate])[0]
            issue = next(item for item in registration["target_bindings"] if item.get("issue_id") == "yield")
            store.save_revision(
                candidate_id=registration["candidate_id"], source_hash=SOURCE_HASH,
                extraction_id="extract-1", expected_parent=0, operation_id="reject-optional-revision",
                recipe=candidate.model_dump(mode="json"), target_bindings=[],
            )
            store.resolve_issue(
                candidate_id=registration["candidate_id"], source_hash=SOURCE_HASH,
                extraction_id="extract-1", revision=1, issue_id="yield", target_id=issue["target_id"],
                disposition="rejected", final_value=None, operation_id="reject-optional-proposal",
            )
            with self.assertRaisesRegex(ReviewConflict, "pending proposals"):
                store.approve_revision(
                    candidate_id=registration["candidate_id"], source_hash=SOURCE_HASH,
                    extraction_id="extract-1", revision=1, operation_id="approve-after-reject",
                    source_path=_source_path(tmp),
                )
            projection = store.project(registration["candidate_id"], SOURCE_HASH, "extract-1", candidate)

        self.assertFalse(projection.import_ready)
        self.assertIsNone(projection.recipe["yield_text"])
        self.assertEqual(projection.recipe["suggestions"][0]["review_status"], "rejected")

    def test_rejected_proposal_does_not_resolve_its_critical_finding(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ReviewStore(Path(tmp) / "review_events.jsonl")
            candidate = RecipeCandidate(
                name="Soup", ingredients=["water"], instructions=["Cook."],
                missing_information=[Diagnostic(
                    field_path="ingredients[0]", kind="ambiguous", detail="Quantity unclear.",
                )],
                suggestions=[Suggestion(
                    suggestion_id="quantity", field_path="ingredients[0]", proposed_value="1 cup water",
                    rationale="Possible quantity.",
                )],
            )
            registration = store.register_candidates("scan", SOURCE_HASH, "extract-1", [candidate])[0]
            issue = next(item for item in registration["target_bindings"]
                         if item.get("issue_id") == "quantity")
            store.save_revision(
                candidate_id=registration["candidate_id"], source_hash=SOURCE_HASH,
                extraction_id="extract-1", expected_parent=0, operation_id="reject-critical-revision",
                recipe=candidate.model_dump(mode="json"), target_bindings=[],
            )
            store.resolve_issue(
                candidate_id=registration["candidate_id"], source_hash=SOURCE_HASH,
                extraction_id="extract-1", revision=1, issue_id="quantity", target_id=issue["target_id"],
                disposition="rejected", final_value=None, operation_id="reject-critical-proposal",
            )

            with self.assertRaisesRegex(ReviewConflict, "pending proposals"):
                store.approve_revision(
                    candidate_id=registration["candidate_id"], source_hash=SOURCE_HASH,
                    extraction_id="extract-1", revision=1, operation_id="reject-critical-approval",
                    source_path=_source_path(tmp),
                )

    def test_backend_approval_rejects_whitespace_only_recipe_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ReviewStore(Path(tmp) / "review_events.jsonl")
            candidate = RecipeCandidate(name="Rice", ingredients=["   "], instructions=["Cook."])
            registration = store.register_candidates("scan", SOURCE_HASH, "extract-1", [candidate])[0]
            store.save_revision(
                candidate_id=registration["candidate_id"], source_hash=SOURCE_HASH,
                extraction_id="extract-1", expected_parent=0, operation_id="whitespace-revision",
                recipe=candidate.model_dump(mode="json"), target_bindings=[],
            )

            with self.assertRaisesRegex(ReviewConflict, "incomplete"):
                store.approve_revision(
                    candidate_id=registration["candidate_id"], source_hash=SOURCE_HASH,
                    extraction_id="extract-1", revision=1, operation_id="whitespace-approval",
                    source_path=_source_path(tmp),
                )

    def test_resolution_requires_matching_source_and_preserves_original_issue(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ReviewStore(Path(tmp) / "review_events.jsonl")
            candidate = RecipeCandidate(
                name="Bars", ingredients=["dates"], instructions=["Mix."],
                suggestions=[Suggestion(
                    suggestion_id="quantity", field_path="ingredients[0]",
                    proposed_value="1 cup dates", rationale="possible reading",
                )],
            )
            registration = store.register_candidates("scan", SOURCE_HASH, "extract-1", [candidate])[0]
            issue = next(item for item in registration["target_bindings"]
                         if item.get("issue_id") == "quantity")
            store.save_revision(
                candidate_id=registration["candidate_id"], source_hash=SOURCE_HASH,
                extraction_id="extract-1", expected_parent=0, operation_id="source-bound-revision",
                recipe=candidate.model_dump(mode="json"), target_bindings=[],
            )
            with self.assertRaisesRegex(ReviewConflict, "source hash"):
                store.resolve_issue(
                    candidate_id=registration["candidate_id"], source_hash="wrong-hash",
                    extraction_id="extract-1", revision=1, issue_id="quantity",
                    target_id=issue["target_id"], disposition="edited", final_value="3/4 cup dates",
                    operation_id="wrong-source-resolution",
                )
            resolved = store.resolve_issue(
                candidate_id=registration["candidate_id"], source_hash=SOURCE_HASH,
                extraction_id="extract-1", revision=1, issue_id="quantity",
                target_id=issue["target_id"], disposition="edited", final_value="3/4 cup dates",
                operation_id="right-source-resolution",
            )

        self.assertEqual(resolved["original_issue"]["original_issue"],
                         candidate.suggestions[0].model_dump(mode="json"))

    def test_not_recipe_content_resolution_requires_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ReviewStore(Path(tmp) / "review_events.jsonl")
            candidate = RecipeCandidate(
                name="Bars", ingredients=["dates"], instructions=["Mix."],
                suggestions=[Suggestion(
                    suggestion_id="ocr-artifact", field_path="ingredients[0]",
                    proposed_value="1 cup dates", rationale="possible reading",
                )],
            )
            registration = store.register_candidates("scan", SOURCE_HASH, "extract-1", [candidate])[0]
            issue = next(item for item in registration["target_bindings"]
                         if item.get("issue_id") == "ocr-artifact")
            store.save_revision(
                candidate_id=registration["candidate_id"], source_hash=SOURCE_HASH,
                extraction_id="extract-1", expected_parent=0, operation_id="not-content-revision",
                recipe=candidate.model_dump(mode="json"), target_bindings=[],
            )
            with self.assertRaisesRegex(ValueError, "evidence"):
                store.resolve_issue(
                    candidate_id=registration["candidate_id"], source_hash=SOURCE_HASH,
                    extraction_id="extract-1", revision=1, issue_id="ocr-artifact",
                    target_id=issue["target_id"], disposition="not_recipe_content", final_value=None,
                    operation_id="unsupported-not-content",
                )

    def test_revision_declares_versioned_content_digest(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ReviewStore(Path(tmp) / "review_events.jsonl")
            candidate = RecipeCandidate(name="Rice", ingredients=["rice"], instructions=["Cook."])
            registration = store.register_candidates("scan", SOURCE_HASH, "extract-1", [candidate])[0]
            revision = store.save_revision(
                candidate_id=registration["candidate_id"], source_hash=SOURCE_HASH,
                extraction_id="extract-1", expected_parent=0, operation_id="versioned-digest",
                recipe=candidate.model_dump(mode="json"), target_bindings=[],
            )

        self.assertEqual(revision["content_digest_version"], 1)

    def test_approval_requires_available_source_bytes_matching_source_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ReviewStore(Path(tmp) / "review_events.jsonl")
            candidate = RecipeCandidate(name="Rice", ingredients=["rice"], instructions=["Cook."])
            registration = store.register_candidates("scan", SOURCE_HASH, "extract-1", [candidate])[0]
            store.save_revision(
                candidate_id=registration["candidate_id"], source_hash=SOURCE_HASH,
                extraction_id="extract-1", expected_parent=0, operation_id="source-check-revision",
                recipe=candidate.model_dump(mode="json"), target_bindings=[],
            )
            mismatched = Path(tmp) / "mismatched.jpg"
            mismatched.write_bytes(b"different scan")
            with self.assertRaisesRegex(ReviewConflict, "do not match"):
                store.approve_revision(
                    candidate_id=registration["candidate_id"], source_hash=SOURCE_HASH,
                    extraction_id="extract-1", revision=1, operation_id="mismatched-source-approval",
                    source_path=mismatched,
                )
            with self.assertRaisesRegex(ReviewConflict, "unavailable"):
                store.approve_revision(
                    candidate_id=registration["candidate_id"], source_hash=SOURCE_HASH,
                    extraction_id="extract-1", revision=1, operation_id="missing-source-approval",
                    source_path=Path(tmp) / "missing.jpg",
                )
            matching_source = _source_path(tmp)
            approved = store.approve_revision(
                candidate_id=registration["candidate_id"], source_hash=SOURCE_HASH,
                extraction_id="extract-1", revision=1, operation_id="matching-source-approval",
                source_path=matching_source,
            )

        self.assertEqual(approved["source_path"], str(matching_source))

    def test_approval_rejects_candidate_with_protected_import_outcome(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "review_events.jsonl"
            store = ReviewStore(path)
            candidate = RecipeCandidate(name="Rice", ingredients=["rice"], instructions=["Cook."])
            registration = store.register_candidates("scan", SOURCE_HASH, "extract-1", [candidate])[0]
            candidate_id = registration["candidate_id"]
            store.save_revision(
                candidate_id=candidate_id, source_hash=SOURCE_HASH,
                extraction_id="extract-1", expected_parent=0, operation_id="protected-import-revision",
                recipe=candidate.model_dump(mode="json"), target_bindings=[],
            )
            import_record = {"key": candidate_id, "identity": candidate_id,
                             "status": "IMPORTED", "title": "Rice", "file": "scan"}
            (Path(tmp) / "import_state.jsonl").write_text(json.dumps(import_record) + "\n", encoding="utf-8")

            with self.assertRaisesRegex(ReviewConflict, "protected import"):
                store.approve_revision(
                    candidate_id=candidate_id, source_hash=SOURCE_HASH,
                    extraction_id="extract-1", revision=1, operation_id="blocked-import-approval",
                    source_path=_source_path(tmp),
                )

    def test_approval_protects_legacy_uncertain_record_matching_title_and_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ReviewStore(Path(tmp) / "review_events.jsonl")
            candidate = RecipeCandidate(name="Rice", ingredients=["rice"], instructions=["Cook."])
            registration = store.register_candidates("scan", SOURCE_HASH, "extract-1", [candidate])[0]
            store.save_revision(
                candidate_id=registration["candidate_id"], source_hash=SOURCE_HASH,
                extraction_id="extract-1", expected_parent=0, operation_id="legacy-protection-revision",
                recipe=candidate.model_dump(mode="json"), target_bindings=[],
            )
            legacy_record = {"key": f"{SOURCE_HASH}:0", "status": "UNCERTAIN",
                             "title": "Rice", "file": "scan"}
            (Path(tmp) / "import_state.jsonl").write_text(json.dumps(legacy_record) + "\n", encoding="utf-8")

            with self.assertRaisesRegex(ReviewConflict, "protected import"):
                store.approve_revision(
                    candidate_id=registration["candidate_id"], source_hash=SOURCE_HASH,
                    extraction_id="extract-1", revision=1, operation_id="blocked-legacy-approval",
                    source_path=_source_path(tmp),
                )

    def test_revision_readback_rejects_event_payload_tampering(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ReviewStore(Path(tmp) / "review_events.jsonl")
            candidate = RecipeCandidate(name="Rice", ingredients=["rice"], instructions=["Cook."])
            registration = store.register_candidates("scan", SOURCE_HASH, "extract-1", [candidate])[0]
            original_append = store._append

            def append_then_tamper(event, *, locked=False):
                original_append(event, locked=locked)
                if event.get("event") == "review_revision":
                    records = [json.loads(line) for line in store.path.read_text(encoding="utf-8").splitlines()]
                    records[-1]["warnings"] = ["changed after append"]
                    store.path.write_text("".join(json.dumps(item) + "\n" for item in records), encoding="utf-8")

            with patch.object(store, "_append", side_effect=append_then_tamper):
                with self.assertRaisesRegex(ReviewConflict, "readback"):
                    store.save_revision(
                        candidate_id=registration["candidate_id"], source_hash=SOURCE_HASH,
                        extraction_id="extract-1", expected_parent=0, operation_id="tampered-revision",
                        recipe=candidate.model_dump(mode="json"), target_bindings=[],
                    )

    def test_save_and_approval_retry_return_same_events_and_stale_parent_conflicts(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ReviewStore(Path(tmp) / "review_events.jsonl")
            candidate = RecipeCandidate(name="Rice", ingredients=["1 cup rice"], instructions=["Cook."])
            registered = store.register_candidates("scan", SOURCE_HASH, "extract-1", [candidate])[0]
            candidate_id = registered["candidate_id"]
            revision = store.save_revision(
                candidate_id=candidate_id, source_hash=SOURCE_HASH, extraction_id="extract-1",
                expected_parent=0, operation_id="save-op",
                recipe={"name": "Rice", "ingredients": ["1 cup rice"], "instructions": ["Cook."]},
                target_bindings=[],
            )
            retried = ReviewStore(store.path).save_revision(
                candidate_id=candidate_id, source_hash=SOURCE_HASH, extraction_id="extract-1",
                expected_parent=0, operation_id="save-op",
                recipe={"name": "Rice", "ingredients": ["1 cup rice"], "instructions": ["Cook."]},
                target_bindings=[],
            )
            approval = store.approve_revision(
                candidate_id=candidate_id, source_hash=SOURCE_HASH, extraction_id="extract-1",
                revision=1, operation_id="approve-op",
                source_path=_source_path(tmp),
            )
            approval_retry = ReviewStore(store.path).approve_revision(
                candidate_id=candidate_id, source_hash=SOURCE_HASH, extraction_id="extract-1",
                revision=1, operation_id="approve-op",
                source_path=_source_path(tmp),
            )

            self.assertEqual(revision["event_id"], retried["event_id"])
            self.assertEqual(approval["event_id"], approval_retry["event_id"])
            with self.assertRaisesRegex(ReviewConflict, "operation_id"):
                store.save_revision(
                    candidate_id=candidate_id, source_hash=SOURCE_HASH, extraction_id="extract-1",
                    expected_parent=0, operation_id="save-op",
                    recipe={"name": "Different", "ingredients": ["2 cups rice"], "instructions": ["Cook."]},
                    target_bindings=[],
                )
            with self.assertRaises(ReviewConflict):
                store.save_revision(
                    candidate_id=candidate_id, source_hash=SOURCE_HASH, extraction_id="extract-1",
                    expected_parent=0, operation_id="save-other",
                    recipe={"name": "Rice", "ingredients": ["2 cups rice"], "instructions": ["Cook."]},
                    target_bindings=[],
                )

    def test_save_and_approval_retries_recover_after_durable_append_readback_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ReviewStore(Path(tmp) / "review_events.jsonl")
            candidate = RecipeCandidate(name="Rice", ingredients=["1 cup rice"], instructions=["Cook."])
            candidate_id = store.register_candidates("scan", SOURCE_HASH, "extract-1", [candidate])[0]["candidate_id"]
            original_append = store._append
            fail_once = {"save": True, "approval": True}

            def append_then_lose_ack(event, *, locked=False):
                original_append(event, locked=locked)
                if event.get("event") == "review_revision" and fail_once["save"]:
                    fail_once["save"] = False
                    raise OSError("simulated lost write acknowledgement")
                if event.get("event") == "review_approval" and fail_once["approval"]:
                    fail_once["approval"] = False
                    raise OSError("simulated lost approval acknowledgement")

            with patch.object(store, "_append", side_effect=append_then_lose_ack):
                with self.assertRaisesRegex(OSError, "lost write acknowledgement"):
                    store.save_revision(
                        candidate_id=candidate_id, source_hash=SOURCE_HASH, extraction_id="extract-1",
                        expected_parent=0, operation_id="ambiguous-save",
                        recipe={"name": "Rice", "ingredients": ["1 cup rice"], "instructions": ["Cook."]},
                        target_bindings=[],
                    )
                revision = store.save_revision(
                    candidate_id=candidate_id, source_hash=SOURCE_HASH, extraction_id="extract-1",
                    expected_parent=0, operation_id="ambiguous-save",
                    recipe={"name": "Rice", "ingredients": ["1 cup rice"], "instructions": ["Cook."]},
                    target_bindings=[],
                )
                with self.assertRaisesRegex(OSError, "lost approval acknowledgement"):
                    store.approve_revision(
                        candidate_id=candidate_id, source_hash=SOURCE_HASH, extraction_id="extract-1",
                        revision=1, operation_id="ambiguous-approval",
                        source_path=_source_path(tmp),
                    )
                approval = store.approve_revision(
                    candidate_id=candidate_id, source_hash=SOURCE_HASH, extraction_id="extract-1",
                    revision=1, operation_id="ambiguous-approval",
                    source_path=_source_path(tmp),
                )
            events = [json.loads(line) for line in store.path.read_text(encoding="utf-8").splitlines()]

        self.assertEqual(revision["revision"], 1)
        self.assertEqual(len([item for item in events if item.get("event") == "review_revision"]), 1)
        self.assertEqual(len([item for item in events if item.get("event") == "review_approval"]), 1)
        self.assertEqual(approval["revision"], 1)

    def test_new_revision_invalidates_prior_exact_revision_approval(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ReviewStore(Path(tmp) / "review_events.jsonl")
            candidate = RecipeCandidate(name="Rice", ingredients=["1 cup rice"], instructions=["Cook."])
            registration = store.register_candidates("scan", SOURCE_HASH, "extract-1", [candidate])[0]
            first = store.save_revision(
                candidate_id=registration["candidate_id"], source_hash=SOURCE_HASH, extraction_id="extract-1",
                expected_parent=0, operation_id="save-first",
                recipe={"name": "Rice", "ingredients": ["1 cup rice"], "instructions": ["Cook."]},
                target_bindings=[],
            )
            store.approve_revision(candidate_id=registration["candidate_id"], source_hash=SOURCE_HASH,
                                    extraction_id="extract-1", revision=1, operation_id="approve-first",
                                    source_path=_source_path(tmp))
            store.save_revision(
                candidate_id=registration["candidate_id"], source_hash=SOURCE_HASH, extraction_id="extract-1",
                expected_parent=1, operation_id="save-second",
                recipe={"name": "Rice", "ingredients": ["2 cups rice"], "instructions": ["Cook."]},
                target_bindings=[],
            )
            projected = store.project(registration["candidate_id"], SOURCE_HASH, "extract-1", candidate)
            events = [json.loads(line)
                      for line in store.path.read_text(encoding="utf-8").splitlines()]

        self.assertEqual(first["revision"], 1)
        self.assertFalse(projected.import_ready)
        self.assertEqual(sum(item.get("event") == "review_approval" for item in events), 1)

    def test_projection_rejects_approved_revision_whose_recipe_snapshot_changed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "review_events.jsonl"
            store = ReviewStore(path)
            candidate = RecipeCandidate(name="Rice", ingredients=["1 cup rice"], instructions=["Cook."])
            registration = store.register_candidates("scan", SOURCE_HASH, "extract-1", [candidate])[0]
            store.save_revision(
                candidate_id=registration["candidate_id"], source_hash=SOURCE_HASH,
                extraction_id="extract-1", expected_parent=0, operation_id="snapshot-integrity-revision",
                recipe=candidate.model_dump(mode="json"), target_bindings=[],
            )
            store.approve_revision(
                candidate_id=registration["candidate_id"], source_hash=SOURCE_HASH,
                extraction_id="extract-1", revision=1, operation_id="snapshot-integrity-approval",
                source_path=_source_path(tmp),
            )
            records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            for event in records:
                if event.get("event") == "review_revision":
                    event["recipe"]["ingredients"] = ["2 cups rice"]
            path.write_text("".join(json.dumps(event) + "\n" for event in records), encoding="utf-8")
            projected = ReviewStore(path).project(registration["candidate_id"], SOURCE_HASH,
                                                  "extract-1", candidate)

        self.assertFalse(projected.approved)
        self.assertFalse(projected.import_ready)

    def test_newer_legacy_draft_invalidates_modern_exact_approval(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ReviewStore(Path(tmp) / "review_events.jsonl")
            candidate = RecipeCandidate(name="Rice", ingredients=["1 cup rice"], instructions=["Cook."])
            registration = store.register_candidates("scan", SOURCE_HASH, "extract-1", [candidate])[0]
            store.save_revision(
                candidate_id=registration["candidate_id"], source_hash=SOURCE_HASH, extraction_id="extract-1",
                expected_parent=0, operation_id="draft-before-approval",
                recipe=candidate.model_dump(mode="json"), target_bindings=[],
            )
            store.approve_revision(
                candidate_id=registration["candidate_id"], source_hash=SOURCE_HASH, extraction_id="extract-1",
                revision=1, operation_id="approval-before-legacy-draft",
                source_path=_source_path(tmp),
            )
            later = (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat()
            legacy_draft = DraftRecord(
                draft_id="legacy:1", candidate_id=registration["candidate_id"], source_hash=SOURCE_HASH,
                source_path="scan.pdf", recipe=candidate.model_dump(mode="json"),
                raw_ingredients="1 cup rice", raw_instructions="Cook.", mode="structured",
                warnings=[], revision=1, saved_at=later,
            )
            projected = store.project(registration["candidate_id"], SOURCE_HASH, "extract-1",
                                      candidate, legacy_draft)

        self.assertFalse(projected.approved)
        self.assertFalse(projected.import_ready)
        self.assertTrue(projected.legacy_draft_conflict)

    def test_approval_rejects_resolution_whose_immutable_row_was_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ReviewStore(Path(tmp) / "review_events.jsonl")
            candidate = RecipeCandidate(
                name="Bars", ingredients=["dates"], instructions=["Mix."],
                suggestions=[Suggestion(
                    suggestion_id="quantity", field_path="ingredients[0]",
                    proposed_value="1 cup dates", rationale="possible reading",
                    confidence="low", uncertainty="cropped",
                )],
            )
            registration = store.register_candidates("scan", SOURCE_HASH, "extract-1", [candidate])[0]
            proposal = next(item for item in registration["target_bindings"]
                            if item.get("issue_id") == "quantity")
            store.save_revision(
                candidate_id=registration["candidate_id"], source_hash=SOURCE_HASH, extraction_id="extract-1",
                expected_parent=0, operation_id="save-without-row",
                recipe={"name": "Bars", "ingredients": ["cranberries"], "instructions": ["Mix."]},
                target_bindings=[{"target_id": "unrelated-row", "field": "ingredients", "position": 0}],
            )
            store.resolve_issue(
                candidate_id=registration["candidate_id"], source_hash=SOURCE_HASH,
                extraction_id="extract-1", revision=1, issue_id="quantity",
                target_id=proposal["target_id"], disposition="edited", final_value="1 cup dates",
                operation_id="resolve-removed-target",
            )

            with self.assertRaisesRegex(ReviewConflict, "source target is not present"):
                store.approve_revision(
                    candidate_id=registration["candidate_id"], source_hash=SOURCE_HASH,
                    extraction_id="extract-1", revision=1, operation_id="approve-removed-target",
                    source_path=_source_path(tmp),
                )

    def test_issue_resolution_on_stale_revision_conflicts(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ReviewStore(Path(tmp) / "review_events.jsonl")
            candidate = RecipeCandidate(
                name="Rice", ingredients=["rice"], instructions=["Cook."],
                suggestions=[Suggestion(
                    suggestion_id="quantity", field_path="ingredients[0]",
                    proposed_value="1 cup rice", rationale="possible reading",
                    confidence="low", uncertainty="unclear",
                )],
            )
            registration = store.register_candidates("scan", SOURCE_HASH, "extract-1", [candidate])[0]
            issue = next(item for item in registration["target_bindings"]
                         if item.get("issue_id") == "quantity")
            store.save_revision(
                candidate_id=registration["candidate_id"], source_hash=SOURCE_HASH, extraction_id="extract-1",
                expected_parent=0, operation_id="revision-one",
                recipe={"name": "Rice", "ingredients": ["rice"], "instructions": ["Cook."]},
                target_bindings=[{"target_id": issue["source_target_id"], "field": "ingredients", "position": 0}],
            )
            store.save_revision(
                candidate_id=registration["candidate_id"], source_hash=SOURCE_HASH, extraction_id="extract-1",
                expected_parent=1, operation_id="revision-two",
                recipe={"name": "Rice", "ingredients": ["rice with salt"], "instructions": ["Cook."]},
                target_bindings=[{"target_id": issue["source_target_id"], "field": "ingredients", "position": 0}],
            )

            with self.assertRaisesRegex(ReviewConflict, "revision changed"):
                store.resolve_issue(
                    candidate_id=registration["candidate_id"], source_hash=SOURCE_HASH,
                    extraction_id="extract-1", revision=1, issue_id="quantity",
                    target_id=issue["target_id"], disposition="accepted", final_value="1 cup rice",
                    operation_id="stale-resolution",
                )

    def test_two_processes_cannot_commit_the_same_parent_revision(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "review_events.jsonl")
            candidate = RecipeCandidate(name="Rice", ingredients=["1 cup rice"], instructions=["Cook."])
            candidate_id = ReviewStore(Path(path)).register_candidates(
                "scan", "source-hash", "extract-1", [candidate]
            )[0]["candidate_id"]
            processes = [subprocess.Popen(
                [sys.executable, "-c", _SAME_PARENT_WORKER,
                 path, candidate_id, f"save-{index}"], cwd=Path.cwd(),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True,
            ) for index in range(2)]
            try:
                ready = [process.stdout.readline().strip() for process in processes]
                self.assertEqual(ready, ["READY", "READY"])
                for process in processes:
                    process.stdin.write("go\n")
                    process.stdin.close()
                outputs = [process.communicate(timeout=15) for process in processes]
                self.assertEqual([process.returncode for process in processes], [0, 0],
                                 [error for _, error in outputs])
                outcomes = [json.loads(stdout.strip().splitlines()[-1])[0]
                            for stdout, _ in outputs]
            finally:
                for process in processes:
                    if process.poll() is None:
                        process.kill()
                        process.wait(timeout=5)

        self.assertCountEqual(outcomes, ["saved", "conflict"])


if __name__ == "__main__":
    unittest.main()
