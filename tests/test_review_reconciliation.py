from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from recipe_quality import ExtractionBatch, RecipeCandidate
from review_store import ReviewConflict, ReviewStore
from reconciliation_service import RetryService, ReconciliationError
from import_recipes import StateStore
from import_protection import protected_candidate_ids
from tests.ui_fixture_factory import write_ui_fixture
from ui_data import load_catalog


class ReviewReconciliationTests(unittest.TestCase):
    def _retry(self, root: Path, candidates):
        fixture = write_ui_fixture(root)

        def extractor(source, mode, config):
            return ExtractionBatch(recipes=candidates)

        return fixture, RetryService(Path(fixture["output"]), extractor=extractor)

    def test_attempt_cache_is_isolated_and_cancel_does_not_publish_new_truth(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            replacement = RecipeCandidate(name="Reordered dish", ingredients=["2 cups rice"],
                                          instructions=["Steam the rice."])
            fixture, service = self._retry(root, [replacement])
            before = sorted(path.name for path in (Path(fixture["output"]) / "extracted").glob("*.json"))
            plan = service.preview(str(fixture["digest"]))
            attempt = service.start_attempt(str(fixture["digest"]), mode="local_ocr")
            service.cancel(attempt["attempt_id"])
            cancelled = service.get_attempt(attempt["attempt_id"])
            after = sorted(path.name for path in (Path(fixture["output"]) / "extracted").glob("*.json"))
            attempt_cache = list((Path(attempt["attempt_dir"]) / "extracted").glob("*.json"))
            active = load_catalog(Path(fixture["output"]))

        self.assertTrue(plan["unresolved"])
        self.assertEqual(before, after)
        self.assertEqual(len(attempt_cache), 1)
        self.assertNotIn("Reordered dish", {view.candidate.name for view in active})
        self.assertEqual(cancelled["status"], "cancelled")

    def test_cloud_attempt_requires_explicit_consent_before_creating_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture, service = self._retry(Path(tmp), [RecipeCandidate(name="Rice")])
            with self.assertRaisesRegex(ValueError, "consent"):
                service.start_attempt(str(fixture["digest"]), mode="gemini")
            self.assertEqual(service.attempts(), [])

    def test_changed_source_bytes_are_a_new_explicit_attempt_lineage(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture, service = self._retry(Path(tmp), [RecipeCandidate(name="Retry", ingredients=["1 cup"], instructions=["Mix."])])
            source = Path(fixture["source"])
            source.write_bytes(source.read_bytes() + b"\nnew scan bytes")
            attempt = service.start_attempt(str(fixture["digest"]), mode="local_ocr")
            comparison = service.compare(attempt["attempt_id"])
            old_target = next(view for view in load_catalog(Path(fixture["output"]))
                              if view.candidate.name == "Fried Rice")
            with self.assertRaisesRegex(ReconciliationError, "Source scan bytes changed"):
                service.apply_mapping(
                    attempt["attempt_id"], comparison["new_candidates"][0]["candidate_id"],
                    old_target.candidate_id, ["ingredients"], old_target.revision,
                    "changed-source-map", "The source was updated.",
                )

        self.assertNotEqual(attempt["attempt_source_hash"], fixture["digest"])
        self.assertTrue(comparison["source_changed"])

    def test_compare_does_not_match_by_array_position_and_duplicate_titles_are_held(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = write_ui_fixture(Path(tmp))
            old_views = load_catalog(Path(fixture["output"]))
            old = next(view for view in old_views if view.candidate.name == "Fried Rice")
            changed = RecipeCandidate(name="Fried Rice", ingredients=["2 cups rice"],
                                      instructions=["Steam the rice."])
            service = RetryService(Path(fixture["output"]), extractor=lambda *_: ExtractionBatch(
                recipes=[changed, old.candidate],
            ))
            attempt = service.start_attempt(str(fixture["digest"]), mode="local_ocr")
            new_views = load_catalog(Path(attempt["attempt_dir"]))
            comparison = service.compare(attempt["attempt_id"])

        self.assertEqual(len(comparison["new_candidates"]), 2)
        exact = next(row for row in comparison["new_candidates"]
                     if row["suggested_match_id"] == old.candidate_id)
        self.assertEqual(exact["match_status"], "unique_content_match")
        changed_row = next(row for row in comparison["new_candidates"]
                           if row["candidate"]["ingredients"] == ["2 cups rice"])
        self.assertIsNone(changed_row["suggested_match_id"])
        self.assertEqual(len(changed_row["title_match_ids"]), 1)
        self.assertEqual({view.candidate.name for view in new_views}, {"Fried Rice"})
        self.assertFalse(comparison["mappings_applied"])

    def test_explicit_mapping_saves_fresh_revision_and_preserves_unselected_corrections(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = write_ui_fixture(root)
            output = Path(fixture["output"])
            views = load_catalog(output)
            target = next(view for view in views if view.candidate.name == "Fried Rice")
            reviews = ReviewStore(output / "review_events.jsonl")
            reviews.save_revision(
                candidate_id=target.candidate_id, source_hash=target.source_hash,
                extraction_id=target.extraction_id, expected_parent=0,
                operation_id="old-correction", recipe={
                    **target.candidate.model_dump(mode="json"),
                    "ingredients": ["3 cups corrected rice", "1 egg"],
                }, target_bindings=[],
            )
            new = RecipeCandidate(name="Fried Rice", ingredients=["4 cups rice"],
                                  instructions=["Steam, then fold in egg."])
            service = RetryService(output, extractor=lambda *_: ExtractionBatch(recipes=[new]))
            attempt = service.start_attempt(str(fixture["digest"]), mode="local_ocr")
            new_id = service.compare(attempt["attempt_id"])["new_candidates"][0]["candidate_id"]
            result = service.apply_mapping(
                attempt["attempt_id"], new_id, target.candidate_id, ["instructions"], 1,
                "map-instructions", "The source shows a revised instruction sequence.",
            )
            projected = ReviewStore(output / "review_events.jsonl").project(
                target.candidate_id, target.source_hash, target.extraction_id, target.candidate,
            )
            active_target = next(view for view in load_catalog(output)
                                 if view.candidate_id == target.candidate_id)
            lineage_recorded = any(
                event.get("event") == "candidate_reconciliation"
                for event in ReviewStore(output / "review_events.jsonl").events
            )

        self.assertEqual(result["revision"], 2)
        self.assertFalse(projected.import_ready)
        self.assertEqual(projected.recipe["ingredients"], ["3 cups corrected rice", "1 egg"])
        self.assertEqual(projected.recipe["instructions"], new.instructions)
        self.assertEqual(active_target.revision, 2)
        self.assertEqual(active_target.candidate.instructions, new.instructions)
        self.assertTrue(lineage_recorded)

    def test_stale_revision_and_protected_target_cannot_be_mapped_or_declared_new(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = write_ui_fixture(root)
            output = Path(fixture["output"])
            target = next(view for view in load_catalog(output) if view.candidate.name == "Fried Rice")
            imported = next(view for view in load_catalog(output)
                            if view.candidate.name == "Imported Recipe")
            candidate = RecipeCandidate(name="Fried Rice", ingredients=["1 cup rice"], instructions=["Cook."])
            service = RetryService(output, extractor=lambda *_: ExtractionBatch(recipes=[candidate]))
            attempt = service.start_attempt(str(fixture["digest"]), mode="local_ocr")
            new_id = service.compare(attempt["attempt_id"])["new_candidates"][0]["candidate_id"]
            with self.assertRaises(ReconciliationError):
                service.apply_mapping(attempt["attempt_id"], new_id, target.candidate_id,
                                      ["ingredients"], 4, "stale-map", "evidence")
            with self.assertRaises(ReconciliationError):
                service.apply_mapping(attempt["attempt_id"], new_id, None,
                                      ["ingredients"], 0, "new-duplicate", "evidence")
            with self.assertRaises(ReconciliationError):
                service.apply_mapping(attempt["attempt_id"], new_id, imported.candidate_id,
                                      ["ingredients"], imported.revision, "map-imported", "evidence")

    def test_renamed_copy_of_imported_recipe_cannot_be_declared_new(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = write_ui_fixture(Path(tmp))
            imported = next(view for view in load_catalog(Path(fixture["output"]))
                            if view.candidate.name == "Imported Recipe")
            renamed = RecipeCandidate(
                name="Renamed Imported Recipe",
                ingredients=list(imported.candidate.ingredients),
                instructions=list(imported.candidate.instructions),
            )
            service = RetryService(Path(fixture["output"]), extractor=lambda *_: ExtractionBatch(
                recipes=[renamed],
            ))
            attempt = service.start_attempt(str(fixture["digest"]), mode="local_ocr")
            new_id = service.compare(attempt["attempt_id"])["new_candidates"][0]["candidate_id"]

            with self.assertRaisesRegex(ReconciliationError, "matches existing source content"):
                service.apply_mapping(
                    attempt["attempt_id"], new_id, None,
                    ["name", "ingredients", "instructions"], 0,
                    "renamed-imported-recipe", "The scan shows the same flour recipe under a revised title.",
                )

    def test_distinct_candidate_cannot_be_declared_new_around_protected_source_sibling(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = write_ui_fixture(Path(tmp))
            output = Path(fixture["output"])
            protected = next(view for view in load_catalog(output)
                             if view.candidate.name == "Fried Rice")
            StateStore(output / "import_state.jsonl").add(
                f"{fixture['digest']}:77", "IMPORTED", "fried-rice",
                str(fixture["source"]), protected.candidate.name,
                identity=protected.candidate_id,
            )
            distinct = RecipeCandidate(
                name="Separate Beans", ingredients=["2 cups beans"], instructions=["Simmer."],
            )
            service = RetryService(output, extractor=lambda *_: ExtractionBatch(recipes=[distinct]))
            attempt = service.start_attempt(str(fixture["digest"]), mode="local_ocr")
            new_id = service.compare(attempt["attempt_id"])["new_candidates"][0]["candidate_id"]

            with self.assertRaisesRegex(ReconciliationError, "protected sibling"):
                service.apply_mapping(
                    attempt["attempt_id"], new_id, None,
                    ["name", "ingredients", "instructions"], 0,
                    "declare-around-import", "A heading and distinct ingredient group appear in the source.",
                )

    def test_new_recipe_declaration_and_defer_are_append_only_and_do_not_publish_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture, service = self._retry(Path(tmp), [RecipeCandidate(
                name="Distinct new dish", ingredients=["1 cup beans"], instructions=["Simmer."],
            )])
            # Keep this positive case scoped to a source without an imported sibling.
            (Path(fixture["output"]) / "import_state.jsonl").unlink()
            attempt = service.start_attempt(str(fixture["digest"]), mode="local_ocr")
            new_id = service.compare(attempt["attempt_id"])["new_candidates"][0]["candidate_id"]
            declared = service.apply_mapping(
                attempt["attempt_id"], new_id, None, ["name", "ingredients", "instructions"],
                0, "declare-new-dish", "A separate heading and ingredient group appear in the scan.",
            )
            deferred = service.defer(attempt["attempt_id"], new_id, "defer-dish", "The boundary is unclear.")
            active_views = load_catalog(Path(fixture["output"]))
            active_names = {view.candidate.name for view in active_views}
            promoted = next(view for view in active_views if view.candidate_id == new_id)
            active_extractions = sorted(path.name for path in
                                        (Path(fixture["output"]) / "extracted").glob("*.json"))
            reviews = ReviewStore(Path(fixture["output"]) / "review_events.jsonl")
            reviews.save_revision(
                candidate_id=promoted.candidate_id, source_hash=promoted.source_hash,
                extraction_id=promoted.extraction_id, expected_parent=0,
                operation_id="declared-new-draft", recipe=promoted.candidate.model_dump(mode="json"),
                target_bindings=[],
            )
            reviews.approve_revision(
                candidate_id=promoted.candidate_id, source_hash=promoted.source_hash,
                extraction_id=promoted.extraction_id, revision=1,
                operation_id="declared-new-approval", source_path=promoted.source_path,
            )
            ready = next(view for view in load_catalog(Path(fixture["output"]))
                         if view.candidate_id == new_id)
            active_extractions_after = sorted(path.name for path in
                                              (Path(fixture["output"]) / "extracted").glob("*.json"))

        self.assertEqual(declared["decision"], "declared_new")
        self.assertEqual(declared["active_projection"], "available_for_review")
        self.assertEqual(deferred["decision"], "deferred")
        self.assertIn("Distinct new dish", active_names)
        self.assertTrue(promoted.source_available)
        self.assertTrue(ready.import_ready)
        self.assertEqual(active_extractions, active_extractions_after)

    def test_failed_extraction_keeps_prior_candidate_inspectable(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = write_ui_fixture(Path(tmp))
            output = Path(fixture["output"])
            original_names = {view.candidate.name for view in load_catalog(output)}

            def fail_extraction(*_):
                raise RuntimeError("synthetic provider failure")

            service = RetryService(output, extractor=fail_extraction)
            attempt = service.start_attempt(str(fixture["digest"]), mode="local_ocr")
            visible_names = {view.candidate.name for view in load_catalog(output)}
            attempt_cache = list((Path(attempt["attempt_dir"]) / "extracted").glob("*.json"))

        self.assertEqual(visible_names, original_names)
        self.assertEqual(attempt["job_status"], "completed_with_errors")
        self.assertEqual(attempt_cache, [])

    def test_import_intent_blocks_new_review_approval(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = write_ui_fixture(Path(tmp))
            output = Path(fixture["output"])
            target = next(view for view in load_catalog(output) if view.candidate.name == "Fried Rice")
            source_path = Path(fixture["source"])
            reviews = ReviewStore(output / "review_events.jsonl")
            reviews.save_revision(
                candidate_id=target.candidate_id, source_hash=target.source_hash,
                extraction_id=target.extraction_id, expected_parent=0, operation_id="intent-approval-revision",
                recipe=target.candidate.model_dump(mode="json"), target_bindings=[],
            )
            (output / "ui_import_events.jsonl").write_text(json.dumps({
                "event": "intent_started", "attempt_id": "in-flight",
                "candidate_id": target.candidate_id,
            }) + "\n", encoding="utf-8")
            with self.assertRaises(ReviewConflict):
                reviews.approve_revision(
                    candidate_id=target.candidate_id, source_hash=target.source_hash,
                    extraction_id=target.extraction_id, revision=1,
                    operation_id="intent-approval", source_path=source_path,
                )

    def test_malformed_attempt_ledger_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "output"
            retry_root = output / "reextraction_attempts"
            retry_root.mkdir(parents=True)
            (retry_root / "events.jsonl").write_text('{"event":"attempt_created"', encoding="utf-8")
            with self.assertRaises(ReconciliationError):
                RetryService(output).attempts()

    def test_started_intent_protects_candidate_until_definite_rejection(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            candidate_id = "candidate-uuid"
            events = output / "ui_import_events.jsonl"
            events.write_text(json.dumps({
                "event": "intent_started", "attempt_id": "attempt-1",
                "candidate_id": candidate_id,
            }) + "\n", encoding="utf-8")
            self.assertIn(candidate_id, protected_candidate_ids(output))
            with events.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"event": "create_rejected", "attempt_id": "attempt-1",
                                         "candidate_id": candidate_id}) + "\n")
            self.assertNotIn(candidate_id, protected_candidate_ids(output))

    def test_catalog_marks_inflight_import_identity_check_mealie(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = write_ui_fixture(Path(tmp))
            output = Path(fixture["output"])
            target = next(view for view in load_catalog(output) if view.candidate.name == "Fried Rice")
            events = output / "ui_import_events.jsonl"
            events.write_text(json.dumps({"event": "intent_started", "attempt_id": "inflight",
                                          "candidate_id": target.candidate_id}) + "\n", encoding="utf-8")
            protected = next(view for view in load_catalog(output)
                             if view.candidate_id == target.candidate_id)

        self.assertEqual(protected.status, "Check Mealie")
        self.assertTrue(protected.protected)
        self.assertEqual(protected.import_record["creation_status"], "uncertain")


if __name__ == "__main__":
    unittest.main()
