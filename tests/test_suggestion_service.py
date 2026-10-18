"""Durable, revision-bound on-demand suggestion requests."""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
import threading
from unittest.mock import patch

from recipe_quality import RecipeCandidate
from review_store import ReviewConflict, ReviewStore, _recipe_digest
import review_store
from suggestion_service import SuggestionService
from model_routing import QuotaExhausted

class SuggestionServiceTests(unittest.TestCase):
    def setUp(self):
        self._temporary_root = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary_root.cleanup)
        self.root = Path(self._temporary_root.name)

    def setup_recipe(self, root):
        path = root / "review_events.jsonl"
        store = ReviewStore(path)
        candidate = RecipeCandidate(name="Rice", ingredients=["1 cup rice"], instructions=[])
        source_hash = hashlib.sha256(b"scan").hexdigest()
        registration = store.register_candidates("scan.jpg", source_hash, "extract-1", [candidate])[0]
        revision = store.save_revision(
            candidate_id=registration["candidate_id"], source_hash=source_hash,
            extraction_id="extract-1", expected_parent=0, operation_id="save-1",
            recipe=candidate.model_dump(mode="json"), target_bindings=registration["target_bindings"],
        )
        return store, registration["candidate_id"], source_hash, revision

    def test_request_requires_explicit_cloud_consent_and_is_idempotent(self):
        with self.subTest("consent and idempotency"):
            root = self.root
            store, candidate_id, source_hash, revision = self.setup_recipe(root)
            service = SuggestionService(root, store=store, proposer=lambda *args: [])
            view = {
                "candidate_id": candidate_id, "source_hash": source_hash,
                "extraction_id": "extract-1", "revision": revision["revision"],
                "content_digest": revision["content_sha256"], "protected": False,
                "source_verified": True, "candidate": RecipeCandidate(
                    name="Rice", ingredients=["1 cup rice"], instructions=[]),
            }
            with self.assertRaisesRegex(ValueError, "consent"):
                service.request(view, consent=False, operation_id="request-op")
            first = service.request(view, consent=True, operation_id="request-op")
            second = service.request(view, consent=True, operation_id="repeated-click")
            self.assertEqual(first["request_id"], second["request_id"])
            self.assertEqual(first["status"], "queued")
            self.assertEqual(set(first["recipe_snapshot"]), {"name", "ingredients", "instructions"})

    def test_request_filters_revision_bindings_to_instruction_targets(self):
        root = self.root
        store, candidate_id, source_hash, revision = self.setup_recipe(root)
        service = SuggestionService(root, store=store, proposer=lambda *args: [])
        candidate = RecipeCandidate(name="Rice", ingredients=["1 cup rice"], instructions=[])
        view = {"candidate_id": candidate_id, "source_hash": source_hash,
                "extraction_id": "extract-1", "revision": 1,
                "content_digest": revision["content_sha256"], "source_verified": True,
                "candidate": candidate, "revision_target_bindings": revision["target_bindings"]}

        request = service.request(view, consent=True, operation_id="filter-all-targets")

        self.assertEqual(request["status"], "queued")
        self.assertTrue(all(target["field"] == "instructions"
                            for target in request["instruction_targets"]))

    def test_text_only_request_does_not_require_source_verification(self):
        store, candidate_id, source_hash, revision = self.setup_recipe(self.root)
        received = []
        service = SuggestionService(
            self.root, store=store,
            proposer=lambda snapshot, kind: received.append((snapshot, kind)) or [],
        )
        view = {
            "candidate_id": candidate_id, "source_hash": source_hash,
            "extraction_id": "extract-1", "revision": revision["revision"],
            "content_digest": revision["content_sha256"], "protected": False,
            "source_verified": False,
            "candidate": RecipeCandidate(name="Rice", ingredients=["1 cup rice"], instructions=[]),
        }

        request = service.request(view, consent=True, operation_id="source-unavailable-request")
        result = service.process(request["request_id"])

        self.assertEqual(result["status"], "completed")
        self.assertEqual(
            {key: received[0][0][key] for key in ("name", "ingredients", "instructions")},
            {"name": "Rice", "ingredients": ["1 cup rice"], "instructions": []},
        )
        self.assertEqual(received[0][0]["instruction_targets"], [])
        self.assertEqual(received[0][1], "instructions")

    def test_loaded_suggestion_indexes_refresh_after_append_without_reloading(self):
        store, candidate_id, source_hash, revision = self.setup_recipe(self.root)
        service = SuggestionService(
            self.root, store=store,
            proposer=lambda *_: [{
                "field_path": "instructions", "proposed_value": "Simmer rice.",
                "classification": "inferred", "rationale": "Likely method",
                "uncertainty": "Timing is unknown.",
                "insertion_location": {"field": "instructions", "after": None},
            }])
        view = {"candidate_id": candidate_id, "source_hash": source_hash,
                "extraction_id": "extract-1", "revision": revision["revision"],
                "content_digest": revision["content_sha256"], "source_verified": True,
                "candidate": RecipeCandidate(name="Rice", ingredients=["1 cup rice"], instructions=[])}
        request = service.request(view, consent=True, operation_id="indexed-request")

        # A catalog already owns this loaded snapshot. Looking up current request
        # metadata must not take another lock/read of the JSONL ledger.
        with patch.object(store, "_reload", side_effect=AssertionError("unexpected ledger reload")):
            self.assertEqual(store.latest_suggestion_request_loaded(
                candidate_id, source_hash, "extract-1")["status"], "queued")
            self.assertEqual(store._suggestion_request_status_loaded(request["request_id"]), "queued")

        result = service.process(request["request_id"])
        self.assertEqual(result["status"], "completed")
        with patch.object(store, "_reload", side_effect=AssertionError("unexpected ledger reload")):
            latest = store.latest_suggestion_request_loaded(candidate_id, source_hash, "extract-1")
            proposals = store._dynamic_suggestions_by_candidate[candidate_id]
            self.assertEqual(latest["status"], "completed")
            self.assertEqual(len(proposals), 1)
            self.assertEqual(proposals[0]["proposal_id"],
                             store._dynamic_suggestions_by_id[proposals[0]["proposal_id"]]["proposal_id"])

        class NoLedgerIteration(list):
            def __iter__(self):
                raise AssertionError("candidate projection scanned the complete review ledger")

        store.events = NoLedgerIteration(store.events)
        effective = store.project_loaded(
            candidate_id, source_hash, "extract-1",
            RecipeCandidate(name="Rice", ingredients=["1 cup rice"], instructions=[]))
        self.assertTrue(any(item.get("suggestion_id") == proposals[0]["proposal_id"]
                            for item in effective.recipe["suggestions"]))
        self.assertFalse(effective.import_ready)

    def test_completed_resolved_request_allows_new_explicit_request_but_same_op_deduplicates(self):
        store, candidate_id, source_hash, revision = self.setup_recipe(self.root)
        proposal_result = [{
            "field_path": "instructions", "proposed_value": "Simmer rice.",
            "classification": "inferred", "rationale": "Likely method",
            "uncertainty": "Timing is unknown.",
            "insertion_location": {"field": "instructions", "after": None},
        }]
        service = SuggestionService(self.root, store=store,
                                    proposer=lambda *_: proposal_result)
        view = {"candidate_id": candidate_id, "source_hash": source_hash,
                "extraction_id": "extract-1", "revision": revision["revision"],
                "content_digest": revision["content_sha256"], "source_verified": True,
                "candidate": RecipeCandidate(name="Rice", ingredients=["1 cup rice"], instructions=[])}
        first = service.request(view, consent=True, operation_id="first-explicit-request")
        service.process(first["request_id"])
        proposal = store.dynamic_suggestions_for(candidate_id, source_hash, "extract-1")[0]
        store.resolve_dynamic_suggestion(
            candidate_id=candidate_id, proposal_id=proposal["proposal_id"],
            disposition="declined", operation_id="decline-first-proposal")

        second = service.request(view, consent=True, operation_id="second-explicit-request")
        repeated = service.request(view, consent=True, operation_id="second-explicit-request")

        self.assertNotEqual(second["request_id"], first["request_id"])
        self.assertEqual(repeated["request_id"], second["request_id"])
        self.assertEqual(second["status"], "queued")

    def test_unresolved_or_empty_completed_request_retry_policy(self):
        store, candidate_id, source_hash, revision = self.setup_recipe(self.root)
        proposal_result = [{
            "field_path": "instructions", "proposed_value": "Simmer rice.",
            "classification": "inferred", "rationale": "Likely method",
            "uncertainty": "Timing is unknown.",
            "insertion_location": {"field": "instructions", "after": None},
        }]
        view = {"candidate_id": candidate_id, "source_hash": source_hash,
                "extraction_id": "extract-1", "revision": revision["revision"],
                "content_digest": revision["content_sha256"], "source_verified": True,
                "candidate": RecipeCandidate(name="Rice", ingredients=["1 cup rice"], instructions=[])}

        service = SuggestionService(self.root, store=store,
                                    proposer=lambda *_: proposal_result)
        unresolved = service.request(view, consent=True, operation_id="unresolved-request")
        service.process(unresolved["request_id"])
        retry_while_unresolved = service.request(
            view, consent=True, operation_id="request-while-unresolved")
        self.assertEqual(retry_while_unresolved["request_id"], unresolved["request_id"])

        empty_root = self.root / "empty-result"
        empty_root.mkdir()
        empty_store, empty_candidate_id, empty_source_hash, empty_revision = self.setup_recipe(empty_root)
        empty_service = SuggestionService(empty_root, store=empty_store, proposer=lambda *_: [])
        empty_view = {"candidate_id": empty_candidate_id, "source_hash": empty_source_hash,
                      "extraction_id": "extract-1", "revision": empty_revision["revision"],
                      "content_digest": empty_revision["content_sha256"], "source_verified": True,
                      "candidate": RecipeCandidate(name="Rice", ingredients=["1 cup rice"], instructions=[])}
        empty_first = empty_service.request(empty_view, consent=True, operation_id="empty-first")
        empty_service.process(empty_first["request_id"])
        empty_second = empty_service.request(empty_view, consent=True, operation_id="empty-second")
        self.assertNotEqual(empty_second["request_id"], empty_first["request_id"])

    def test_provider_result_becomes_stale_after_revision_changes(self):
        with self.subTest("stale provider result"):
            root = self.root
            store, candidate_id, source_hash, revision = self.setup_recipe(root)
            source = root / "source.jpg"
            source.write_bytes(b"scan")
            produced = []
            service = SuggestionService(root, store=store,
                                        proposer=lambda snapshot, kind: produced.append(snapshot) or [{
                                            "field_path": "instructions", "proposed_value": "Simmer rice.",
                                            "classification": "inferred", "rationale": "Likely method",
                                            "uncertainty": "Timing is unknown.",
                                            "insertion_location": {"field": "instructions", "after": None},
                                        }])
            view = {"candidate_id": candidate_id, "source_hash": source_hash,
                    "extraction_id": "extract-1", "revision": 1,
                    "content_digest": revision["content_sha256"], "protected": False,
                    "source_verified": True,
                    "candidate": RecipeCandidate(name="Rice", ingredients=["1 cup rice"], instructions=[])}
            request = service.request(view, consent=True, operation_id="request-op")
            store.save_revision(candidate_id=candidate_id, source_hash=source_hash,
                                extraction_id="extract-1", expected_parent=1, operation_id="save-2",
                                recipe={"name": "Rice", "ingredients": ["2 cups rice"],
                                        "instructions": ["Cook rice."]},
                                target_bindings=revision["target_bindings"])
            result = service.process(request["request_id"])
            self.assertEqual(result["status"], "stale")
            stored = store.dynamic_suggestions_for(candidate_id, source_hash, "extract-1")
            self.assertEqual(stored[0]["status"], "stale")
            with self.assertRaisesRegex(ReviewConflict, "pending generated proposals"):
                store.approve_revision(candidate_id=candidate_id, source_hash=source_hash,
                                       extraction_id="extract-1", revision=2, operation_id="approve-stale",
                                       source_path=source)
            store.discard_stale_dynamic_suggestion(candidate_id=candidate_id,
                                                   proposal_id=stored[0]["proposal_id"],
                                                   operation_id="discard-stale")
            store.approve_revision(candidate_id=candidate_id, source_hash=source_hash,
                                   extraction_id="extract-1", revision=2, operation_id="approve-after-discard",
                                   source_path=source)

    def test_same_candidate_duplicate_request_does_not_call_provider_twice(self):
        with self.subTest("duplicate provider request"):
            root = self.root
            store, candidate_id, source_hash, revision = self.setup_recipe(root)
            calls = []
            service = SuggestionService(root, store=store,
                                        proposer=lambda snapshot, kind: calls.append(1) or [{
                                            "field_path": "instructions", "proposed_value": "Cook rice.",
                                            "classification": "inferred", "rationale": "Common method",
                                            "uncertainty": "Not specified.",
                                            "insertion_location": {"field": "instructions", "after": None},
                                        }])
            view = {"candidate_id": candidate_id, "source_hash": source_hash,
                    "extraction_id": "extract-1", "revision": 1,
                    "content_digest": revision["content_sha256"], "protected": False,
                    "source_verified": True,
                    "candidate": RecipeCandidate(name="Rice", ingredients=["1 cup rice"], instructions=[])}
            request = service.request(view, consent=True, operation_id="request-op")
            service.process(request["request_id"])
            service.process(request["request_id"])
            self.assertEqual(calls, [1])

    def test_new_pending_request_invalidates_an_existing_approval(self):
        root = self.root
        source = root / "source.jpg"
        source.write_bytes(b"scan")
        store = ReviewStore(root / "review_events.jsonl")
        candidate = RecipeCandidate(name="Rice", ingredients=["1 cup rice"], instructions=["Cook rice."])
        source_hash = hashlib.sha256(b"scan").hexdigest()
        registration = store.register_candidates(str(source), source_hash, "extract-1", [candidate])[0]
        revision = store.save_revision(candidate_id=registration["candidate_id"], source_hash=source_hash,
                                        extraction_id="extract-1", expected_parent=0, operation_id="save-1",
                                        recipe=candidate.model_dump(mode="json"),
                                        target_bindings=registration["target_bindings"])
        store.approve_revision(candidate_id=registration["candidate_id"], source_hash=source_hash,
                               extraction_id="extract-1", revision=1, operation_id="approve-1",
                               source_path=source)
        self.assertTrue(store.project(registration["candidate_id"], source_hash, "extract-1", candidate).import_ready)
        view = {"candidate_id": registration["candidate_id"], "source_hash": source_hash,
                "extraction_id": "extract-1", "revision": 1, "content_digest": revision["content_sha256"],
                "source_verified": True, "candidate": candidate}
        SuggestionService(root, store=store, proposer=lambda *args: []).request(
            view, consent=True, operation_id="request-1")
        projection = store.project(registration["candidate_id"], source_hash, "extract-1", candidate)
        self.assertFalse(projection.approved)
        self.assertFalse(projection.import_ready)
        with self.assertRaisesRegex(ReviewConflict, "still in progress"):
            store.approve_revision(candidate_id=registration["candidate_id"], source_hash=source_hash,
                                   extraction_id="extract-1", revision=1, operation_id="approve-2",
                                   source_path=source)

    def test_generated_proposal_application_saves_revision_and_decision_atomically(self):
        root = self.root
        source = root / "source.jpg"
        source.write_bytes(b"scan")
        store = ReviewStore(root / "review_events.jsonl")
        candidate = RecipeCandidate(name="Rice", ingredients=["rice"], instructions=[])
        source_hash = hashlib.sha256(b"scan").hexdigest()
        registration = store.register_candidates(str(source), source_hash, "extract-1", [candidate])[0]
        revision = store.save_revision(candidate_id=registration["candidate_id"], source_hash=source_hash,
                                        extraction_id="extract-1", expected_parent=0, operation_id="save-1",
                                        recipe=candidate.model_dump(mode="json"),
                                        target_bindings=registration["target_bindings"])
        service = SuggestionService(root, store=store, proposer=lambda *args: [{
            "field_path": "instructions", "proposed_value": "Simmer the rice.",
            "classification": "inferred", "rationale": "Common preparation",
            "uncertainty": "Time is not specified.",
            "insertion_location": {"field": "instructions", "after": None},
        }])
        view = {"candidate_id": registration["candidate_id"], "source_hash": source_hash,
                "extraction_id": "extract-1", "revision": 1,
                "content_digest": revision["content_sha256"], "source_verified": True,
                "candidate": candidate}
        request = service.request(view, consent=True, operation_id="request-1")
        service.process(request["request_id"])
        proposal = store.dynamic_suggestions_for(registration["candidate_id"], source_hash, "extract-1")[0]
        updated = candidate.model_copy(update={"instructions": ["Simmer the rice."]})
        saved, decision = store.apply_dynamic_suggestion(
            candidate_id=registration["candidate_id"], proposal_id=proposal["proposal_id"],
            expected_parent=1, recipe=updated.model_dump(mode="json"),
            target_bindings=registration["target_bindings"], disposition="accepted",
            final_value="Simmer the rice.", operation_id="apply-1")
        projected = store.project(registration["candidate_id"], source_hash, "extract-1", candidate)
        self.assertEqual(saved["revision"], 2)
        self.assertEqual(decision["application_revision"], 2)
        self.assertEqual(projected.recipe["instructions"], ["Simmer the rice."])
        self.assertEqual(projected.recipe["suggestions"][0]["dynamic_status"], "accepted")
        self.assertEqual(len(projected.recipe["suggestions"]), 1)
        self.assertFalse(projected.approved)

    def test_second_same_request_proposal_needs_explicit_rebase_after_first_apply(self):
        root = self.root
        store = ReviewStore(root / "review_events.jsonl")
        candidate = RecipeCandidate(name="Pasta", ingredients=["water", "pasta"],
                                    instructions=["Boil water.", "Cook pasta."])
        source_hash = hashlib.sha256(b"two steps").hexdigest()
        registration = store.register_candidates("scan.jpg", source_hash, "extract-1", [candidate])[0]
        bindings = [
            {"target_id": "step-one", "target_kind": "row", "field": "instructions", "position": 0},
            {"target_id": "step-two", "target_kind": "row", "field": "instructions", "position": 1},
        ]
        revision = store.save_revision(
            candidate_id=registration["candidate_id"], source_hash=source_hash,
            extraction_id="extract-1", expected_parent=0, operation_id="two-step-revision",
            recipe=candidate.model_dump(mode="json"), target_bindings=bindings,
        )
        service = SuggestionService(root, store=store, proposer=lambda *args: [
            {"field_path": "instructions", "target_id": "step-one",
             "proposed_value": "Bring water to a boil.", "classification": "inferred",
             "rationale": "Clarifies action.", "uncertainty": "Wording is inferred."},
            {"field_path": "instructions", "target_id": "step-two",
             "proposed_value": "Cook pasta until tender.", "classification": "inferred",
             "rationale": "Adds a doneness cue.", "uncertainty": "Doneness is inferred."},
        ])
        view = {"candidate_id": registration["candidate_id"], "source_hash": source_hash,
                "extraction_id": "extract-1", "revision": 1,
                "content_digest": revision["content_sha256"], "source_verified": True,
                "candidate": candidate, "revision_target_bindings": revision["target_bindings"]}
        request = service.request(view, consent=True, operation_id="two-proposals-request")
        service.process(request["request_id"])
        proposals = store.dynamic_suggestions_for(registration["candidate_id"], source_hash, "extract-1")
        first = next(item for item in proposals if item["target_id"] == "step-one")
        second = next(item for item in proposals if item["target_id"] == "step-two")

        first_recipe = candidate.model_copy(update={
            "instructions": ["Bring water to a boil.", "Cook pasta."]})
        store.apply_dynamic_suggestion(
            candidate_id=registration["candidate_id"], proposal_id=first["proposal_id"],
            expected_parent=1, recipe=first_recipe.model_dump(mode="json"),
            target_bindings=revision["target_bindings"], disposition="accepted",
            final_value="Bring water to a boil.", operation_id="apply-first-step",
        )
        projected = store.project(registration["candidate_id"], source_hash, "extract-1", candidate)

        self.assertEqual(next(item for item in projected.recipe["suggestions"]
                              if item["suggestion_id"] == second["proposal_id"])["dynamic_status"], "stale")
        with self.assertRaisesRegex(ReviewConflict, "stale and requires an explicit rebase decision"):
            store.apply_dynamic_suggestion(
                candidate_id=registration["candidate_id"], proposal_id=second["proposal_id"],
                expected_parent=2, recipe=candidate.model_copy(update={
                    "instructions": ["Bring water to a boil.", "Cook pasta until tender."]}).model_dump(mode="json"),
                target_bindings=revision["target_bindings"], disposition="accepted",
                final_value="Cook pasta until tender.", operation_id="apply-second-step",
            )
        source_path = root / "source.jpg"
        source_path.write_bytes(b"two steps")
        current_revision = store.revisions[registration["candidate_id"]][-1]
        current_candidate = RecipeCandidate.model_validate(
            store.project(registration["candidate_id"], source_hash, "extract-1", candidate).recipe)
        rebased_recipe = current_candidate.model_copy(update={
            "instructions": ["Bring water to a boil.", "Cook pasta until tender."]})
        saved, decision = store.rebase_and_apply_dynamic_suggestion(
            candidate_id=registration["candidate_id"], proposal_id=second["proposal_id"],
            expected_parent=2, expected_content_digest=current_revision["content_sha256"],
            recipe=rebased_recipe.model_dump(mode="json"),
            target_bindings=revision["target_bindings"], disposition="accepted",
            final_value="Cook pasta until tender.", source_path=source_path,
            confirmed=True, operation_id="rebase-apply-second-step",
        )
        final_projection = store.project(registration["candidate_id"], source_hash, "extract-1", candidate)
        final_status = next(item for item in final_projection.recipe["suggestions"]
                            if item["suggestion_id"] == second["proposal_id"])

        self.assertEqual(saved["revision"], 3)
        self.assertEqual(decision["rebase_from_revision"], 1)
        self.assertEqual(decision["rebase_to_revision"], 2)
        self.assertEqual(decision["comparison_current_value"], "Cook pasta.")
        self.assertEqual(final_projection.recipe["instructions"],
                         ["Bring water to a boil.", "Cook pasta until tender."])
        self.assertEqual(final_status["dynamic_status"], "accepted")

    def test_stale_rebase_requires_confirmation_live_target_and_current_parent(self):
        root = self.root
        store = ReviewStore(root / "review_events.jsonl")
        candidate = RecipeCandidate(name="Pasta", ingredients=["water", "pasta"],
                                    instructions=["Boil water.", "Cook pasta."])
        source_bytes = b"two steps negative cases"
        source_hash = hashlib.sha256(source_bytes).hexdigest()
        registration = store.register_candidates("scan.jpg", source_hash, "extract-1", [candidate])[0]
        bindings = [
            {"target_id": "step-one", "target_kind": "row", "field": "instructions", "position": 0},
            {"target_id": "step-two", "target_kind": "row", "field": "instructions", "position": 1},
        ]
        revision = store.save_revision(
            candidate_id=registration["candidate_id"], source_hash=source_hash,
            extraction_id="extract-1", expected_parent=0, operation_id="negative-two-step-revision",
            recipe=candidate.model_dump(mode="json"), target_bindings=bindings,
        )
        service = SuggestionService(root, store=store, proposer=lambda *args: [
            {"field_path": "instructions", "target_id": "step-one",
             "proposed_value": "Bring water to a boil.", "classification": "inferred",
             "rationale": "Clarifies action.", "uncertainty": "Wording is inferred."},
            {"field_path": "instructions", "target_id": "step-two",
             "proposed_value": "Cook pasta until tender.", "classification": "inferred",
             "rationale": "Adds a doneness cue.", "uncertainty": "Doneness is inferred."},
        ])
        view = {"candidate_id": registration["candidate_id"], "source_hash": source_hash,
                "extraction_id": "extract-1", "revision": 1,
                "content_digest": revision["content_sha256"], "source_verified": True,
                "candidate": candidate, "revision_target_bindings": revision["target_bindings"]}
        request = service.request(view, consent=True, operation_id="negative-two-proposals-request")
        service.process(request["request_id"])
        proposals = store.dynamic_suggestions_for(registration["candidate_id"], source_hash, "extract-1")
        first = next(item for item in proposals if item["target_id"] == "step-one")
        second = next(item for item in proposals if item["target_id"] == "step-two")
        store.apply_dynamic_suggestion(
            candidate_id=registration["candidate_id"], proposal_id=first["proposal_id"],
            expected_parent=1,
            recipe=candidate.model_copy(update={"instructions": ["Bring water to a boil.", "Cook pasta."]}).model_dump(mode="json"),
            target_bindings=revision["target_bindings"], disposition="accepted",
            final_value="Bring water to a boil.", operation_id="negative-apply-first",
        )
        source_path = root / "source.jpg"
        source_path.write_bytes(source_bytes)
        latest = store.revisions[registration["candidate_id"]][-1]
        recipe = candidate.model_copy(update={
            "instructions": ["Bring water to a boil.", "Cook pasta until tender."]}).model_dump(mode="json")
        args = dict(
            candidate_id=registration["candidate_id"], proposal_id=second["proposal_id"],
            recipe=recipe, target_bindings=revision["target_bindings"], disposition="accepted",
            final_value="Cook pasta until tender.", source_path=source_path,
            insertion_location=None,
        )

        with self.assertRaisesRegex(ValueError, "explicit comparison"):
            store.rebase_and_apply_dynamic_suggestion(
                **args, expected_parent=2, expected_content_digest=latest["content_sha256"],
                confirmed=False, operation_id="negative-unconfirmed-rebase",
            )
        with self.assertRaisesRegex(ReviewConflict, "preserve all current row bindings"):
            store.rebase_and_apply_dynamic_suggestion(
                **{**args, "target_bindings": [revision["target_bindings"][0]]},
                expected_parent=2, expected_content_digest=latest["content_sha256"],
                confirmed=True, operation_id="negative-dropped-binding-rebase",
            )
        with self.assertRaisesRegex(ReviewConflict, "preserve all current row bindings"):
            store.rebase_and_apply_dynamic_suggestion(
                **{**args, "target_bindings": [
                    {"target_id": "rebound-step-one", "target_kind": "row",
                     "field": "instructions", "position": 0}, revision["target_bindings"][1]]},
                expected_parent=2, expected_content_digest=latest["content_sha256"],
                confirmed=True, operation_id="negative-rebound-binding-rebase",
            )
        with self.assertRaisesRegex(ReviewConflict, "recipe changed during comparison"):
            store.rebase_and_apply_dynamic_suggestion(
                **args, expected_parent=2, expected_content_digest="wrong-digest",
                confirmed=True, operation_id="negative-wrong-digest-rebase",
            )
        with self.assertRaisesRegex(ReviewConflict, "recipe changed during comparison"):
            store.rebase_and_apply_dynamic_suggestion(
                **args, expected_parent=1, expected_content_digest=revision["content_sha256"],
                confirmed=True, operation_id="negative-raced-rebase",
            )
        store.save_revision(
            candidate_id=registration["candidate_id"], source_hash=source_hash,
            extraction_id="extract-1", expected_parent=2, operation_id="delete-target-step-two",
            recipe=candidate.model_copy(update={"instructions": ["Bring water to a boil."]}).model_dump(mode="json"),
            target_bindings=[revision["target_bindings"][0]],
        )
        latest_after_delete = store.revisions[registration["candidate_id"]][-1]
        with self.assertRaisesRegex(ReviewConflict, "target was removed"):
            store.rebase_and_apply_dynamic_suggestion(
                **args, expected_parent=3, expected_content_digest=latest_after_delete["content_sha256"],
                confirmed=True, operation_id="negative-deleted-target-rebase",
            )

    def test_stale_insertion_rebase_requires_explicit_current_end(self):
        root = self.root
        store = ReviewStore(root / "review_events.jsonl")
        candidate = RecipeCandidate(name="Tea", ingredients=["water", "tea"], instructions=["Boil water."])
        source_bytes = b"tea steps"
        source_hash = hashlib.sha256(source_bytes).hexdigest()
        registration = store.register_candidates("scan.jpg", source_hash, "extract-1", [candidate])[0]
        bindings = [{"target_id": "step-one", "target_kind": "row",
                     "field": "instructions", "position": 0}]
        revision = store.save_revision(
            candidate_id=registration["candidate_id"], source_hash=source_hash,
            extraction_id="extract-1", expected_parent=0, operation_id="insertion-revision",
            recipe=candidate.model_dump(mode="json"), target_bindings=bindings,
        )
        service = SuggestionService(root, store=store, proposer=lambda *args: [
            {"field_path": "instructions", "target_id": "step-one",
             "proposed_value": "Bring the water to a full boil.", "classification": "inferred",
             "rationale": "Clarifies the endpoint.", "uncertainty": "Wording is inferred."},
            {"field_path": "instructions", "proposed_value": "Steep the tea.",
             "classification": "inferred", "rationale": "Likely next step.",
             "uncertainty": "Steeping time is unspecified.",
             "insertion_location": {"field": "instructions", "after": None}},
        ])
        view = {"candidate_id": registration["candidate_id"], "source_hash": source_hash,
                "extraction_id": "extract-1", "revision": 1,
                "content_digest": revision["content_sha256"], "source_verified": True,
                "candidate": candidate, "revision_target_bindings": revision["target_bindings"]}
        request = service.request(view, consent=True, operation_id="insertion-request")
        service.process(request["request_id"])
        proposals = store.dynamic_suggestions_for(registration["candidate_id"], source_hash, "extract-1")
        first = next(item for item in proposals if item["target_id"] == "step-one")
        insertion = next(item for item in proposals if not item["target_id"])
        source_path = root / "source.jpg"
        source_path.write_bytes(source_bytes)
        store.apply_dynamic_suggestion(
            candidate_id=registration["candidate_id"], proposal_id=first["proposal_id"],
            expected_parent=1,
            recipe=candidate.model_copy(update={"instructions": ["Bring the water to a full boil."]}).model_dump(mode="json"),
            target_bindings=revision["target_bindings"], disposition="accepted",
            final_value="Bring the water to a full boil.", operation_id="apply-first-insertion-set",
        )
        current = store.revisions[registration["candidate_id"]][-1]
        current_candidate = RecipeCandidate.model_validate(
            store.project(registration["candidate_id"], source_hash, "extract-1", candidate).recipe)
        rebased = current_candidate.model_copy(update={"instructions": [
            "Bring the water to a full boil.", "Steep the tea."]}).model_dump(mode="json")
        new_bindings = [*revision["target_bindings"],
                        {"target_id": "new-step", "target_kind": "row",
                         "field": "instructions", "position": 1}]
        with self.assertRaisesRegex(ReviewConflict, "current end"):
            store.rebase_and_apply_dynamic_suggestion(
                candidate_id=registration["candidate_id"], proposal_id=insertion["proposal_id"],
                expected_parent=2, expected_content_digest=current["content_sha256"],
                recipe=rebased, target_bindings=new_bindings, disposition="accepted",
                final_value="Steep the tea.", source_path=source_path, confirmed=True,
                insertion_location={"field": "instructions", "index": 0},
                operation_id="bad-insertion-rebase",
            )
        with self.assertRaisesRegex(ReviewConflict, "preserve all current row bindings and add one row"):
            store.rebase_and_apply_dynamic_suggestion(
                candidate_id=registration["candidate_id"], proposal_id=insertion["proposal_id"],
                expected_parent=2, expected_content_digest=current["content_sha256"],
                recipe=rebased, target_bindings=[*new_bindings,
                    {"target_id": "extra-row", "target_kind": "row",
                     "field": "instructions", "position": 0}], disposition="accepted",
                final_value="Steep the tea.", source_path=source_path, confirmed=True,
                insertion_location={"field": "instructions", "index": 1},
                operation_id="extra-insertion-binding",
            )
        with self.assertRaisesRegex(ReviewConflict, "duplicate row identity"):
            store.rebase_and_apply_dynamic_suggestion(
                candidate_id=registration["candidate_id"], proposal_id=insertion["proposal_id"],
                expected_parent=2, expected_content_digest=current["content_sha256"],
                recipe=rebased, target_bindings=[*new_bindings,
                    {"target_id": "new-step", "target_kind": "row",
                     "field": "instructions", "position": 0}], disposition="accepted",
                final_value="Steep the tea.", source_path=source_path, confirmed=True,
                insertion_location={"field": "instructions", "index": 1},
                operation_id="duplicate-insertion-binding",
            )
        saved, decision = store.rebase_and_apply_dynamic_suggestion(
            candidate_id=registration["candidate_id"], proposal_id=insertion["proposal_id"],
            expected_parent=2, expected_content_digest=current["content_sha256"],
            recipe=rebased, target_bindings=new_bindings, disposition="accepted",
            final_value="Steep the tea.", source_path=source_path, confirmed=True,
            insertion_location={"field": "instructions", "index": 1},
            operation_id="good-insertion-rebase",
        )
        self.assertEqual(saved["revision"], 3)
        self.assertEqual(decision["comparison_insertion_location"],
                         {"field": "instructions", "index": 1})

    def test_provider_quota_and_missing_credentials_are_terminal_without_proposals(self):
        root = self.root
        store, candidate_id, source_hash, revision = self.setup_recipe(root)
        view = {"candidate_id": candidate_id, "source_hash": source_hash,
                "extraction_id": "extract-1", "revision": 1,
                "content_digest": revision["content_sha256"], "source_verified": True,
                "candidate": RecipeCandidate(name="Rice", ingredients=["1 cup rice"], instructions=[])}
        quota_service = SuggestionService(root, store=store,
                                          proposer=lambda *args: (_ for _ in ()).throw(QuotaExhausted("daily quota")))
        quota_request = quota_service.request(view, consent=True, operation_id="quota-request")
        self.assertEqual(quota_service.process(quota_request["request_id"])["status"], "quota_blocked")
        quota_retry = quota_service.request(view, consent=True, operation_id="explicit-quota-retry")
        self.assertNotEqual(quota_request["request_id"], quota_retry["request_id"])
        missing_root = root / "missing-key"
        missing_root.mkdir()
        missing_store, missing_id, missing_hash, missing_revision = self.setup_recipe(missing_root)
        missing_view = {"candidate_id": missing_id, "source_hash": missing_hash,
                        "extraction_id": "extract-1", "revision": 1,
                        "content_digest": missing_revision["content_sha256"], "source_verified": True,
                        "candidate": RecipeCandidate(name="Rice", ingredients=["1 cup rice"], instructions=[])}
        missing_service = SuggestionService(missing_root, store=missing_store)
        missing_request = missing_service.request(missing_view, consent=True, operation_id="missing-key-request")
        with patch.dict("os.environ", {"GEMINI_API_KEY": ""}):
            result = missing_service.process(missing_request["request_id"])
        self.assertEqual(result["status"], "failed")
        self.assertIn("not configured", result["error"])
        self.assertEqual(missing_store.dynamic_suggestions_for(missing_id, missing_hash, "extract-1"), [])

    def test_cancelled_provider_result_is_not_published(self):
        root = self.root
        store, candidate_id, source_hash, revision = self.setup_recipe(root)
        entered = threading.Event()
        release = threading.Event()
        def proposer(snapshot, kind):
            entered.set()
            release.wait(3)
            return [{"field_path": "instructions", "proposed_value": "Cook rice.",
                     "classification": "inferred", "rationale": "Common method",
                     "uncertainty": "Unknown.",
                     "insertion_location": {"field": "instructions", "after": None}}]
        service = SuggestionService(root, store=store, proposer=proposer)
        view = {"candidate_id": candidate_id, "source_hash": source_hash,
                "extraction_id": "extract-1", "revision": 1,
                "content_digest": revision["content_sha256"], "source_verified": True,
                "candidate": RecipeCandidate(name="Rice", ingredients=["1 cup rice"], instructions=[])}
        request = service.request(view, consent=True, operation_id="cancel-request")
        worker = threading.Thread(target=service.process, args=(request["request_id"],))
        worker.start()
        self.assertTrue(entered.wait(3))
        service.cancel(request["request_id"])
        release.set()
        worker.join(3)
        self.assertEqual(service.status(request["request_id"])["status"], "cancelled")
        self.assertEqual(store.dynamic_suggestions_for(candidate_id, source_hash, "extract-1"), [])

    def test_restart_marks_inflight_request_interrupted_without_retry(self):
        root = self.root
        store, candidate_id, source_hash, revision = self.setup_recipe(root)
        view = {"candidate_id": candidate_id, "source_hash": source_hash,
                "extraction_id": "extract-1", "revision": 1,
                "content_digest": revision["content_sha256"], "source_verified": True,
                "candidate": RecipeCandidate(name="Rice", ingredients=["1 cup rice"], instructions=[])}
        service = SuggestionService(root, store=store, proposer=lambda *args: [])
        request = service.request(view, consent=True, operation_id="interrupted-request")
        store.update_suggestion_request(request["request_id"], "running")
        restarted = SuggestionService(root, store=ReviewStore(store.path), proposer=lambda *args: [])
        self.assertEqual(restarted.status(request["request_id"])["status"], "interrupted")
        self.assertEqual(restarted.process(request["request_id"])["status"], "interrupted")

    def test_worker_marks_control_flow_exit_interrupted_before_propagating(self):
        class WorkerStop(BaseException):
            pass

        root = self.root
        store, candidate_id, source_hash, revision = self.setup_recipe(root)
        view = {"candidate_id": candidate_id, "source_hash": source_hash,
                "extraction_id": "extract-1", "revision": 1,
                "content_digest": revision["content_sha256"], "source_verified": True,
                "candidate": RecipeCandidate(name="Rice", ingredients=["1 cup rice"], instructions=[])}
        service = SuggestionService(root, store=store,
                                    proposer=lambda *args: (_ for _ in ()).throw(WorkerStop()))
        request = service.request(view, consent=True, operation_id="worker-control-flow-exit")

        with self.assertRaises(WorkerStop):
            service.process(request["request_id"])

        self.assertEqual(service.status(request["request_id"])["status"], "interrupted")

    def test_restart_discovers_and_resumes_durable_queued_request(self):
        root = self.root
        store, candidate_id, source_hash, revision = self.setup_recipe(root)
        view = {"candidate_id": candidate_id, "source_hash": source_hash,
                "extraction_id": "extract-1", "revision": 1,
                "content_digest": revision["content_sha256"], "source_verified": True,
                "candidate": RecipeCandidate(name="Rice", ingredients=["1 cup rice"], instructions=[])}
        first = SuggestionService(root, store=store, proposer=lambda *args: [])
        queued = first.request(view, consent=True, operation_id="queued-before-thread")

        restarted = SuggestionService(root, store=ReviewStore(store.path), proposer=lambda *args: [])
        discovered = restarted.latest_request(candidate_id, source_hash, "extract-1")
        self.assertEqual(discovered["request_id"], queued["request_id"])
        self.assertEqual(discovered["status"], "queued")
        self.assertEqual(restarted.start_background(discovered["request_id"]).join(3), None)
        self.assertEqual(restarted.status(queued["request_id"])["status"], "completed")

    def test_restart_does_not_interrupt_running_worker_in_another_service(self):
        root = self.root
        store, candidate_id, source_hash, revision = self.setup_recipe(root)
        view = {"candidate_id": candidate_id, "source_hash": source_hash,
                "extraction_id": "extract-1", "revision": 1,
                "content_digest": revision["content_sha256"], "source_verified": True,
                "candidate": RecipeCandidate(name="Rice", ingredients=["1 cup rice"], instructions=[])}
        service = SuggestionService(root, store=store, proposer=lambda *args: [])
        queued = service.request(view, consent=True, operation_id="active-worker")
        store.claim_suggestion_request(queued["request_id"], "active-test-worker")

        restarted = SuggestionService(root, store=ReviewStore(store.path), proposer=lambda *args: [])
        self.assertEqual(restarted.status(queued["request_id"])["status"], "running")

    def test_windows_worker_check_queries_exit_code_without_signaling_process(self):
        import ctypes

        class ApiFunction:
            def __init__(self, callback):
                self.callback = callback

            def __call__(self, *args):
                return self.callback(*args)

        class FakeKernel:
            def __init__(self, exit_code):
                self.exit_code = exit_code
                self.open_access = None
                self.opened_pid = None
                self.closed = []
                self.OpenProcess = ApiFunction(self.open_process)
                self.GetExitCodeProcess = ApiFunction(self.get_exit_code)
                self.CloseHandle = ApiFunction(lambda handle: self.closed.append(handle) or 1)

            def open_process(self, access, inherit, pid):
                self.open_access = access
                self.opened_pid = pid
                return 99

            def get_exit_code(self, handle, output):
                ctypes.cast(output, ctypes.POINTER(ctypes.c_ulong)).contents.value = self.exit_code
                return 1

        active = FakeKernel(259)
        exited = FakeKernel(0)
        self.assertTrue(review_store._windows_process_is_alive(123, active))
        self.assertFalse(review_store._windows_process_is_alive(123, exited))
        self.assertEqual(active.opened_pid, 123)
        self.assertEqual(active.open_access, 0x1000 | 0x00100000)
        self.assertEqual(active.closed, [99])
        self.assertEqual(exited.closed, [99])

        unavailable = FakeKernel(259)
        unavailable.open_process = lambda access, inherit, pid: 0
        unavailable.OpenProcess = ApiFunction(unavailable.open_process)
        with patch("review_store.ctypes.get_last_error", return_value=87):
            self.assertFalse(review_store._windows_process_is_alive(123, unavailable))
        with patch("review_store.ctypes.get_last_error", return_value=5):
            self.assertTrue(review_store._windows_process_is_alive(123, unavailable))
        with patch("review_store.ctypes.get_last_error", return_value=6):
            self.assertTrue(review_store._windows_process_is_alive(123, unavailable))

    def test_protected_import_history_blocks_backend_request(self):
        root = self.root
        store, candidate_id, source_hash, revision = self.setup_recipe(root)
        state = root / "import_state.jsonl"
        state.write_text(json.dumps({"identity": candidate_id, "status": "IMPORTED"}) + "\n",
                         encoding="utf-8")
        protected_store = ReviewStore(store.path, state_path=state)
        view = {"candidate_id": candidate_id, "source_hash": source_hash,
                "extraction_id": "extract-1", "revision": 1,
                "content_digest": revision["content_sha256"], "source_verified": True,
                "candidate": RecipeCandidate(name="Rice", ingredients=["1 cup rice"], instructions=[])}
        service = SuggestionService(root, store=protected_store, proposer=lambda *args: [])
        with self.assertRaisesRegex(ReviewConflict, "protected recipes"):
            service.request(view, consent=True, operation_id="protected-request")

    def test_same_title_candidates_keep_independent_request_snapshots(self):
        root = self.root
        store = ReviewStore(root / "review_events.jsonl")
        candidates = [RecipeCandidate(name="Rice", ingredients=["1 cup rice"], instructions=[]),
                      RecipeCandidate(name="Rice", ingredients=["2 cups rice"], instructions=[])]
        source_hash = hashlib.sha256(b"same scan").hexdigest()
        registrations = store.register_candidates("scan.jpg", source_hash, "extract-1", candidates)
        self.assertNotEqual(registrations[0]["candidate_id"], registrations[1]["candidate_id"])
        views = []
        for index, candidate in enumerate(candidates):
            revision = store.save_revision(candidate_id=registrations[index]["candidate_id"],
                                            source_hash=source_hash, extraction_id="extract-1",
                                            expected_parent=0, operation_id=f"save-{index}",
                                            recipe=candidate.model_dump(mode="json"),
                                            target_bindings=registrations[index]["target_bindings"])
            views.append({"candidate_id": registrations[index]["candidate_id"],
                          "source_hash": source_hash, "extraction_id": "extract-1", "revision": 1,
                          "content_digest": revision["content_sha256"], "source_verified": True,
                          "candidate": candidate})
        service = SuggestionService(root, store=store, proposer=lambda *args: [])
        first = service.request(views[0], consent=True, operation_id="first")
        second = service.request(views[1], consent=True, operation_id="second")
        self.assertNotEqual(first["request_id"], second["request_id"])
        self.assertEqual(first["candidate_id"], views[0]["candidate_id"])
        self.assertEqual(second["recipe_snapshot"]["ingredients"], ["2 cups rice"])

    def test_provider_error_redacts_api_secret(self):
        root = self.root
        store, candidate_id, source_hash, revision = self.setup_recipe(root)
        view = {"candidate_id": candidate_id, "source_hash": source_hash,
                "extraction_id": "extract-1", "revision": 1,
                "content_digest": revision["content_sha256"], "source_verified": True,
                "candidate": RecipeCandidate(name="Rice", ingredients=["1 cup rice"], instructions=[])}
        service = SuggestionService(root, store=store,
                                    proposer=lambda *args: (_ for _ in ()).throw(RuntimeError("provider key-secret failed")))
        request = service.request(view, consent=True, operation_id="secret-request")
        with patch.dict("os.environ", {"GEMINI_API_KEY": "key-secret"}):
            result = service.process(request["request_id"])
        self.assertEqual(result["status"], "failed")
        self.assertNotIn("key-secret", result["error"])

    def test_text_only_provider_cannot_claim_source_supported_recovery(self):
        root = self.root
        store, candidate_id, source_hash, revision = self.setup_recipe(root)
        view = {"candidate_id": candidate_id, "source_hash": source_hash,
                "extraction_id": "extract-1", "revision": 1,
                "content_digest": revision["content_sha256"], "source_verified": True,
                "candidate": RecipeCandidate(name="Rice", ingredients=["1 cup rice"], instructions=[])}
        service = SuggestionService(root, store=store, proposer=lambda *args: [{
            "field_path": "instructions", "proposed_value": "Cook rice.",
            "classification": "source_supported", "rationale": "Model claim",
            "uncertainty": "Unknown.", "insertion_location": {"field": "instructions", "after": None},
        }])
        request = service.request(view, consent=True, operation_id="source-claim-request")
        self.assertEqual(service.process(request["request_id"])["status"], "failed")
        self.assertEqual(store.dynamic_suggestions_for(candidate_id, source_hash, "extract-1"), [])

    def test_malformed_batch_is_rejected_before_any_proposal_is_appended(self):
        root = self.root
        source = root / "source.jpg"
        source.write_bytes(b"scan")
        store = ReviewStore(root / "review_events.jsonl")
        candidate = RecipeCandidate(name="Rice", ingredients=["rice"], instructions=["Cook rice."])
        source_hash = hashlib.sha256(b"scan").hexdigest()
        registration = store.register_candidates(str(source), source_hash, "extract-1", [candidate])[0]
        revision = store.save_revision(candidate_id=registration["candidate_id"], source_hash=source_hash,
                                        extraction_id="extract-1", expected_parent=0, operation_id="save-1",
                                        recipe=candidate.model_dump(mode="json"),
                                        target_bindings=registration["target_bindings"])
        store.approve_revision(candidate_id=registration["candidate_id"], source_hash=source_hash,
                               extraction_id="extract-1", revision=1, operation_id="approve-1",
                               source_path=source)
        service = SuggestionService(root, store=store, proposer=lambda *args: [
            {"field_path": "instructions", "proposed_value": "Add a step.",
             "classification": "inferred", "rationale": "Likely", "uncertainty": "Unknown",
             "insertion_location": {"field": "instructions", "index": 1}},
            {"field_path": "instructions", "proposed_value": "Invalid second proposal",
             "classification": "inferred", "uncertainty": "Missing rationale",
             "insertion_location": {"field": "instructions", "index": 1}},
        ])
        view = {"candidate_id": registration["candidate_id"], "source_hash": source_hash,
                "extraction_id": "extract-1", "revision": 1,
                "content_digest": revision["content_sha256"], "source_verified": True,
                "candidate": candidate}
        request = service.request(view, consent=True, operation_id="malformed-batch")
        self.assertEqual(service.process(request["request_id"])["status"], "failed")
        self.assertEqual(store.dynamic_suggestions_for(registration["candidate_id"], source_hash, "extract-1"), [])
        self.assertTrue(store.project(registration["candidate_id"], source_hash, "extract-1", candidate).import_ready)

    def test_cancel_at_publication_boundary_prevents_any_proposal(self):
        root = self.root
        store, candidate_id, source_hash, revision = self.setup_recipe(root)
        service = SuggestionService(root, store=store, proposer=lambda *args: [{
            "field_path": "instructions", "proposed_value": "Cook rice.",
            "classification": "inferred", "rationale": "Likely", "uncertainty": "Unknown",
            "insertion_location": {"field": "instructions", "after": None},
        }])
        view = {"candidate_id": candidate_id, "source_hash": source_hash,
                "extraction_id": "extract-1", "revision": 1,
                "content_digest": revision["content_sha256"], "source_verified": True,
                "candidate": RecipeCandidate(name="Rice", ingredients=["1 cup rice"], instructions=[])}
        request = service.request(view, consent=True, operation_id="late-cancel")
        entered = threading.Event()
        release = threading.Event()
        original = store.record_dynamic_proposals
        def pause_before_store_commit(request_id, proposals):
            entered.set()
            release.wait(3)
            return original(request_id, proposals)
        from unittest.mock import patch as mock_patch
        with mock_patch.object(store, "record_dynamic_proposals", side_effect=pause_before_store_commit):
            worker = threading.Thread(target=service.process, args=(request["request_id"],))
            worker.start()
            self.assertTrue(entered.wait(3))
            service.cancel(request["request_id"])
            release.set()
            worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(service.status(request["request_id"])["status"], "cancelled")
        self.assertEqual(store.dynamic_suggestions_for(candidate_id, source_hash, "extract-1"), [])
