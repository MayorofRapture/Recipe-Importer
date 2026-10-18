from __future__ import annotations

import tempfile
import unittest
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from import_recipes import file_digest
from recipe_quality import ExtractionBatch, RecipeCandidate, Suggestion, schema_recipe
from review_store import ReviewStore, extraction_identity
from tests.ui_fixture_factory import write_ui_fixture
from ui_data import load_catalog
from ui_import_service import ImportService


class FakeMealie:
    supports_empty_instructions = True

    def __init__(self, *, create_error=None, tag_error=None):
        self.create_error = create_error
        self.tag_error = tag_error
        self.created = []
        self.tags = []

    def create_recipe(self, recipe):
        self.created.append(schema_recipe(recipe))
        if self.create_error:
            raise self.create_error
        return f"recipe-{len(self.created)}"

    def ensure_tag(self, name):
        if self.tag_error:
            raise self.tag_error
        return {"name": name, "slug": name.casefold().replace(" ", "-")}

    def add_tag(self, slug, tag):
        if self.tag_error:
            raise self.tag_error
        self.tags.append((slug, tag["name"]))
        return "tagged"

    def get_recipe(self, slug):
        return {"id": "recipe-id", "slug": slug, "name": "Fried Rice"}


def _approved_fried_rice(root: Path) -> tuple[Path, str, str]:
    fixture = write_ui_fixture(root)
    output = Path(fixture["output"])
    source = Path(fixture["source"])
    digest = file_digest(source)
    catalog = load_catalog(output, Path(fixture["source"]).parent)
    view = next(item for item in catalog if item.candidate.name == "Fried Rice")
    reviews = ReviewStore(output / "review_events.jsonl")
    recipe = view.candidate.model_copy(deep=True)
    recipe.ingredients[0] = "3 cups cooked rice"
    saved = reviews.save_revision(
        candidate_id=view.candidate_id, source_hash=digest, extraction_id=view.extraction_id,
        expected_parent=0, operation_id="approved-test-edit",
        recipe=recipe.model_dump(mode="json"), target_bindings=list(view.target_bindings),
    )
    reviews.approve_revision(
        candidate_id=view.candidate_id, source_hash=digest, extraction_id=view.extraction_id,
        revision=saved["revision"], operation_id="approved-test-consent", source_path=source,
    )
    return output, str(source.parent), view.candidate_id


class UiImportServiceTests(unittest.TestCase):
    def _linked_extraction_pair(self, root: Path) -> tuple[Path, Path, str, str, Path, str, object, object]:
        fixture = write_ui_fixture(root)
        output, source = Path(fixture["output"]), Path(fixture["source"])
        source_root = source.parent
        (output / "import_state.jsonl").write_text("", encoding="utf-8")
        catalog = load_catalog(output, source_root)
        first = next(view for view in catalog if view.candidate.name == "Fried Rice")
        reviews = ReviewStore(output / "review_events.jsonl")
        first_revision = reviews.save_revision(
            candidate_id=first.candidate_id, source_hash=first.source_hash,
            extraction_id=first.extraction_id, expected_parent=0,
            operation_id="linked-import-first-revision",
            recipe=first.candidate.model_dump(mode="json"),
            target_bindings=list(first.target_bindings),
        )
        reviews.approve_revision(
            candidate_id=first.candidate_id, source_hash=first.source_hash,
            extraction_id=first.extraction_id, revision=first_revision["revision"],
            operation_id="linked-import-first-approval", source_path=source,
        )

        second_candidate = first.candidate.model_copy(update={"suggestions": [Suggestion(
            suggestion_id="alternate-yield-proposal", field_path="yield_text",
            proposed_value="2 servings", rationale="Synthetic alternative extraction proposal.",
        )]})
        batch = ExtractionBatch(recipes=[second_candidate])
        extraction_id = extraction_identity(first.source_hash, batch.model_dump(mode="json"))
        cache_path = output / "extracted" / f"{first.source_hash}--linked-version.json"
        second_cache = json.dumps({"schema_version": 2, "source_hash": first.source_hash,
                                   "batch": batch.model_dump(mode="json")})
        cache_path.write_text(second_cache, encoding="utf-8")
        registration = reviews.register_candidates(str(source), first.source_hash,
                                                   extraction_id, [second_candidate])[0]
        second_id = registration["candidate_id"]
        if second_id == first.candidate_id:
            self.fail("Different extraction suggestions must retain a distinct candidate ID.")
        if reviews.identity_link_status(second_id, first.source_hash, extraction_id) != "registered":
            diagnostic = next((item for item in reviews.identity_reconciliation_candidates(first.source_hash)
                               if item["candidate_id"] == second_id), None)
            self.assertTrue(diagnostic and diagnostic.get("eligible"), json.dumps(diagnostic, sort_keys=True))
            reviews.confirm_identity_match(second_id, first.candidate_id)

        bindings = registration["target_bindings"]
        proposal = next(item for item in bindings if item.get("issue_id") == "alternate-yield-proposal")
        second_revision = reviews.save_revision(
            candidate_id=second_id, source_hash=first.source_hash, extraction_id=extraction_id,
            expected_parent=0, operation_id="linked-import-second-revision",
            recipe=second_candidate.model_dump(mode="json"), target_bindings=bindings,
        )
        reviews.resolve_issue(
            candidate_id=second_id, source_hash=first.source_hash, extraction_id=extraction_id,
            revision=second_revision["revision"], issue_id="alternate-yield-proposal",
            target_id=proposal["target_id"], disposition="not_recipe_content", final_value=None,
            evidence_reference="Synthetic reviewer declined optional yield proposal.",
            operation_id="linked-import-second-proposal-decline",
        )
        reviews.approve_revision(
            candidate_id=second_id, source_hash=first.source_hash,
            extraction_id=extraction_id, revision=second_revision["revision"],
            operation_id="linked-import-second-approval", source_path=source,
        )
        self.assertEqual(reviews.identity_link_status(second_id, first.source_hash, extraction_id), "registered")
        # Preserve both exact extraction projections for concurrent plans.
        cache_path.unlink()
        first_view = next(view for view in load_catalog(output, source_root)
                          if view.candidate_id == first.candidate_id)
        cache_path.write_text(second_cache, encoding="utf-8")
        second_view = next(view for view in load_catalog(output, source_root)
                           if view.candidate_id == second_id)
        return output, source_root, first.candidate_id, second_id, cache_path, second_cache, first_view, second_view

    def test_linked_extraction_versions_cannot_create_two_recipes_in_either_order(self):
        for reverse in (False, True):
            with self.subTest(reverse=reverse), tempfile.TemporaryDirectory() as tmp:
                output, source_root, first, second, second_cache_path, second_cache, _, _ = self._linked_extraction_pair(Path(tmp))
                client = FakeMealie()
                service = ImportService(output, source_root=source_root, client=client)
                first_order = [second, first] if reverse else [first, second]
                outcomes = []
                if not reverse:
                    second_cache_path.unlink()
                for position, candidate_id in enumerate(first_order):
                    if position == 1:
                        if reverse:
                            second_cache_path.unlink()
                        else:
                            second_cache_path.write_text(second_cache, encoding="utf-8")
                    plan = service.preview([candidate_id])
                    if position == 0:
                        self.assertEqual(len(plan["eligible"]), 1,
                                         json.dumps(plan["excluded"], sort_keys=True))
                        outcomes.extend(service.dispatch(plan["plan_id"]))
                    else:
                        self.assertEqual(plan["eligible"], [])
                        self.assertIn("linked extraction", plan["excluded"][0]["reason"])

                self.assertEqual(len(client.created), 1)
                self.assertEqual([result["creation_status"] for result in outcomes], ["created"])

    def test_linked_extraction_intent_blocks_concurrent_plan_before_second_create(self):
        for reverse in (False, True):
            with self.subTest(reverse=reverse), tempfile.TemporaryDirectory() as tmp:
                (output, source_root, first, second, _, _, first_view,
                 second_view) = self._linked_extraction_pair(Path(tmp))
                plans = [first_view, second_view] if not reverse else [second_view, first_view]

                class PausedMealie(FakeMealie):
                    def __init__(self):
                        super().__init__()
                        self.started = __import__("threading").Event()
                        self.release = __import__("threading").Event()

                    def create_recipe(self, recipe):
                        self.started.set()
                        if not self.release.wait(5):
                            raise TimeoutError("test create release timed out")
                        return super().create_recipe(recipe)

                client = PausedMealie()
                service = ImportService(output, source_root=source_root, client=client)
                with patch("ui_import_service.load_catalog", return_value=[first_view, second_view]):
                    plan_ids = [service.preview([view.candidate_id])["plan_id"] for view in plans]
                    with ThreadPoolExecutor(max_workers=2) as pool:
                        first_dispatch = pool.submit(service.dispatch, plan_ids[0])
                        self.assertTrue(client.started.wait(5), "first create intent was not dispatched")
                        second_dispatch = pool.submit(service.dispatch, plan_ids[1])
                        second_result = second_dispatch.result(timeout=5)
                        client.release.set()
                        first_result = first_dispatch.result(timeout=5)

                outcomes = [first_result[0], second_result[0]]
                self.assertEqual(len(client.created), 1)
                self.assertEqual(sum(item["creation_status"] == "created" for item in outcomes), 1)
                protected = [item for item in outcomes if item["creation_status"] != "created"]
                self.assertEqual(len(protected), 1)
                self.assertIn("linked extraction", protected[0]["reason"])
    def test_mealie_empty_instructions_rejection_is_not_reposted_for_same_revision(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1] /
                                         ".superpowers/sdd/2026-10-18-manual-review-missing-instructions-navigation/tmp") as tmp:
            fixture = write_ui_fixture(Path(tmp))
            output = Path(fixture["output"])
            source = Path(fixture["source"])
            view = next(item for item in load_catalog(output, source.parent)
                        if item.candidate.name == "Fried Rice")
            recipe = view.candidate.model_copy(deep=True)
            recipe.instructions = []
            reviews = ReviewStore(output / "review_events.jsonl")
            saved = reviews.save_revision(
                candidate_id=view.candidate_id, source_hash=view.source_hash,
                extraction_id=view.extraction_id, expected_parent=0,
                operation_id="save-empty-directions-rejection-test",
                recipe=recipe.model_dump(mode="json"), target_bindings=list(view.target_bindings),
            )
            reviews.record_no_instructions_decision(
                candidate_id=view.candidate_id, source_hash=view.source_hash,
                extraction_id=view.extraction_id, revision=saved["revision"],
                reason="Dry ingredient blend", operation_id="reject-test-no-instructions",
            )
            reviews.approve_revision(
                candidate_id=view.candidate_id, source_hash=view.source_hash,
                extraction_id=view.extraction_id, revision=saved["revision"],
                operation_id="reject-test-approval", source_path=source,
            )

            class Response:
                status_code = 422

            class Http422(RuntimeError):
                response = Response()

            client = FakeMealie(create_error=Http422("unsupported empty instructions"))
            service = ImportService(output, source_root=source.parent, client=client)
            plan = service.preview([view.candidate_id])
            first = service.dispatch(plan["plan_id"])
            again = service.dispatch(plan["plan_id"])
            refreshed = service.preview([view.candidate_id])

            self.assertEqual(first[0]["creation_status"], "rejected")
            self.assertEqual(again[0]["creation_status"], "not_sent")
            self.assertIn("empty-instructions", again[0]["reason"])
            self.assertEqual(len(client.created), 1)
            self.assertEqual(refreshed["eligible"], [])
            self.assertIn("empty-instructions", refreshed["excluded"][0]["reason"])

    def test_manual_approval_of_source_unavailable_seasoning_reaches_mock_mealie_unchanged(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1] /
                                         ".superpowers/sdd/2026-10-18-manual-review-missing-instructions-navigation/tmp") as tmp:
            fixture = write_ui_fixture(Path(tmp))
            output = Path(fixture["output"])
            source = Path(fixture["source"])
            source_hash = file_digest(source)
            view = next(item for item in load_catalog(output, source.parent)
                        if item.candidate.name == "Fried Rice")
            recipe = view.candidate.model_copy(deep=True)
            recipe.name = "Mediterranean Blend"
            recipe.ingredients = ["1 tsp oregano", "1 tsp thyme"]
            recipe.instructions = []
            reviews = ReviewStore(output / "review_events.jsonl")
            saved = reviews.save_revision(
                candidate_id=view.candidate_id, source_hash=source_hash,
                extraction_id=view.extraction_id, expected_parent=0,
                operation_id="save-no-direction-seasoning",
                recipe=recipe.model_dump(mode="json"), target_bindings=[],
            )
            reviews.record_no_instructions_decision(
                candidate_id=view.candidate_id, source_hash=source_hash,
                extraction_id=view.extraction_id, revision=saved["revision"],
                reason="Dry ingredient blend", operation_id="decision-no-directions",
            )
            source.unlink()
            reviews.approve_revision(
                candidate_id=view.candidate_id, source_hash=source_hash,
                extraction_id=view.extraction_id, revision=saved["revision"],
                operation_id="manual-approval-no-source", source_path=source,
                approval_method="manual_source_unavailable", reviewer_acknowledged=True,
                review_rationale="Reviewed the current recipe without the original scan.",
            )
            unsupported = ImportService(output, source_root=source.parent).preview([view.candidate_id])
            service = ImportService(output, source_root=source.parent, client=FakeMealie())
            plan = service.preview([view.candidate_id])
            outcomes = service.dispatch(plan["plan_id"])

            self.assertEqual(len(plan["eligible"]), 1)
            self.assertEqual(unsupported["eligible"], [])
            self.assertIn("disabled", unsupported["excluded"][0]["reason"])
            self.assertEqual(plan["eligible"][0]["approval_method"], "manual_source_unavailable")
            self.assertEqual(plan["eligible"][0]["no_instructions_reason"], "Dry ingredient blend")
            self.assertEqual(plan["eligible"][0]["recipe"]["instructions"], [])
            self.assertEqual(service.client.created[0]["recipeInstructions"], [])
            self.assertEqual(outcomes[0]["creation_status"], "created")
            history = service.history()[0]
            self.assertEqual(history["approval_method"], "manual_source_unavailable")
            self.assertEqual(history["no_instructions_reason"], "Dry ingredient blend")

    def test_preview_freezes_only_selected_exact_approved_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            output, source_root, candidate_id = _approved_fried_rice(Path(tmp))
            service = ImportService(output, source_root=Path(source_root))

            plan = service.preview([candidate_id], batch_tag="Weeknight")

        self.assertEqual(len(plan["eligible"]), 1)
        self.assertEqual(plan["eligible"][0]["candidate_id"], candidate_id)
        self.assertEqual(plan["eligible"][0]["recipe"]["ingredients"][0], "3 cups cooked rice")
        self.assertEqual(plan["batch_tag"], "Weeknight")

    def test_preview_excludes_unapproved_selection_with_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = write_ui_fixture(Path(tmp))
            output = Path(fixture["output"])
            source_root = Path(fixture["source"]).parent
            views = load_catalog(output, source_root)
            unapproved = next(item for item in views if item.revision == 0)

            plan = ImportService(output, source_root=source_root).preview([unapproved.candidate_id])

        self.assertEqual(plan["eligible"], [])
        self.assertEqual(plan["excluded"][0]["candidate_id"], unapproved.candidate_id)
        self.assertIn("approved", plan["excluded"][0]["reason"].lower())

    def test_dispatch_uses_exact_edited_snapshot_and_does_not_import_siblings(self):
        with tempfile.TemporaryDirectory() as tmp:
            output, source_root, candidate_id = _approved_fried_rice(Path(tmp))
            client = FakeMealie()
            service = ImportService(output, source_root=Path(source_root), client=client)
            plan = service.preview([candidate_id], batch_tag="Weeknight")

            outcomes = service.dispatch(plan["plan_id"])

        self.assertEqual(len(client.created), 1)
        self.assertEqual(client.created[0]["recipeIngredient"][0], "3 cups cooked rice")
        self.assertEqual(outcomes[0]["creation_status"], "created")
        self.assertEqual(client.tags, [("recipe-1", "Weeknight")])

    def test_repeated_dispatch_never_posts_a_confirmed_candidate_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            output, source_root, candidate_id = _approved_fried_rice(Path(tmp))
            client = FakeMealie()
            service = ImportService(output, source_root=Path(source_root), client=client)
            plan = service.preview([candidate_id])
            first = service.dispatch(plan["plan_id"])
            again = service.dispatch(plan["plan_id"])

        self.assertEqual(len(client.created), 1)
        self.assertEqual(first[0]["creation_status"], "created")
        self.assertIn(again[0]["creation_status"], {"already_created", "protected"})

    def test_create_timeout_is_uncertain_and_never_retried(self):
        with tempfile.TemporaryDirectory() as tmp:
            output, source_root, candidate_id = _approved_fried_rice(Path(tmp))
            client = FakeMealie(create_error=TimeoutError("socket timed out"))
            service = ImportService(output, source_root=Path(source_root), client=client)
            plan = service.preview([candidate_id])
            first = service.dispatch(plan["plan_id"])
            second = service.dispatch(plan["plan_id"])

        self.assertEqual(first[0]["creation_status"], "uncertain")
        self.assertEqual(second[0]["creation_status"], "uncertain")
        self.assertEqual(len(client.created), 1)

    def test_server_5xx_is_uncertain_and_definite_rejection_can_be_retried(self):
        with tempfile.TemporaryDirectory() as tmp:
            output, source_root, candidate_id = _approved_fried_rice(Path(tmp))
            client = FakeMealie(create_error=RuntimeError("Mealie HTTP 503: private body"))
            service = ImportService(output, source_root=Path(source_root), client=client)
            plan = service.preview([candidate_id])
            first = service.dispatch(plan["plan_id"])
            client.create_error = RuntimeError("Mealie HTTP 422: private recipe details")
            rejected = service.dispatch(plan["plan_id"])

        self.assertEqual(first[0]["creation_status"], "uncertain")
        self.assertEqual(rejected[0]["creation_status"], "uncertain")
        self.assertEqual(len(client.created), 1)

    def test_definite_create_rejection_is_sanitized_and_retryable(self):
        with tempfile.TemporaryDirectory() as tmp:
            output, source_root, candidate_id = _approved_fried_rice(Path(tmp))
            client = FakeMealie(create_error=RuntimeError("Mealie HTTP 422: secret recipe payload"))
            service = ImportService(output, source_root=Path(source_root), client=client)
            plan = service.preview([candidate_id])
            first = service.dispatch(plan["plan_id"])
            client.create_error = None
            second = service.dispatch(plan["plan_id"])

        self.assertEqual(first[0]["creation_status"], "rejected")
        self.assertNotIn("secret", str(first))
        self.assertEqual(second[0]["creation_status"], "created")
        self.assertEqual(len(client.created), 2)

    def test_stale_approval_after_preflight_is_not_sent(self):
        with tempfile.TemporaryDirectory() as tmp:
            output, source_root, candidate_id = _approved_fried_rice(Path(tmp))
            client = FakeMealie()
            service = ImportService(output, source_root=Path(source_root), client=client)
            plan = service.preview([candidate_id])
            catalog = load_catalog(output, Path(source_root))
            view = next(item for item in catalog if item.candidate_id == candidate_id)
            reviews = ReviewStore(output / "review_events.jsonl")
            changed = view.candidate.model_copy(deep=True)
            changed.instructions[0] = "Changed after preflight."
            reviews.save_revision(
                candidate_id=candidate_id, source_hash=view.source_hash,
                extraction_id=view.extraction_id, expected_parent=view.revision,
                operation_id="edit-after-preflight", recipe=changed.model_dump(mode="json"),
                target_bindings=list(view.revision_target_bindings),
            )

            outcomes = service.dispatch(plan["plan_id"])

        self.assertEqual(client.created, [])
        self.assertEqual(outcomes[0]["creation_status"], "not_sent")

    def test_two_service_instances_claim_candidate_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            output, source_root, candidate_id = _approved_fried_rice(Path(tmp))
            client = FakeMealie()
            first_service = ImportService(output, source_root=Path(source_root), client=client)
            second_service = ImportService(output, source_root=Path(source_root), client=client)
            plan = first_service.preview([candidate_id])

            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda service: service.dispatch(plan["plan_id"]),
                                        (first_service, second_service)))

        self.assertEqual(len(client.created), 1)
        statuses = {result[0]["creation_status"] for result in results}
        self.assertTrue(statuses <= {"created", "in_progress", "already_created", "protected", "uncertain"})

    def test_two_processes_cannot_post_the_same_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            output, source_root, candidate_id = _approved_fried_rice(Path(tmp))
            plan = ImportService(output, source_root=Path(source_root)).preview([candidate_id])
            calls = Path(tmp) / "post_calls.txt"
            script = r'''
import sys, time
from pathlib import Path
from ui_import_service import ImportService
class Client:
    def __init__(self, path): self.path = Path(path)
    def create_recipe(self, recipe):
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(recipe.name + "\n")
            handle.flush()
        time.sleep(0.25)
        return "same-recipe"
    def ensure_tag(self, name): return {"name": name}
    def add_tag(self, slug, tag): return "tagged"
import json
print(json.dumps(ImportService(Path(sys.argv[1]), source_root=Path(sys.argv[2]),
              client=Client(sys.argv[3])).dispatch(sys.argv[4])))
'''
            args = [sys.executable, "-c", script, str(output), str(source_root),
                    str(calls), plan["plan_id"]]
            first = subprocess.Popen(args, cwd=Path.cwd(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            second = subprocess.Popen(args, cwd=Path.cwd(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            first_stdout, first_stderr = first.communicate(timeout=20)
            second_stdout, second_stderr = second.communicate(timeout=20)
            self.assertEqual(first.returncode, 0, first_stderr)
            self.assertEqual(second.returncode, 0, second_stderr)
            self.assertTrue(calls.exists(), (first_stdout, second_stdout))
            self.assertEqual(len(calls.read_text(encoding="utf-8").splitlines()), 1,
                             (first_stdout, second_stdout))

    def test_tag_failure_is_repaired_without_recipe_creation(self):
        with tempfile.TemporaryDirectory() as tmp:
            output, source_root, candidate_id = _approved_fried_rice(Path(tmp))
            client = FakeMealie(tag_error=RuntimeError("tag update failed"))
            service = ImportService(output, source_root=Path(source_root), client=client)
            plan = service.preview([candidate_id], batch_tag="Weeknight")
            first = service.dispatch(plan["plan_id"])
            client.tag_error = None
            repaired = service.repair_tag(candidate_id)
            repaired_again = service.repair_tag(candidate_id)

        self.assertEqual(first[0]["creation_status"], "created")
        self.assertEqual(first[0]["tag_status"], "failed")
        self.assertEqual(repaired["tag_status"], "tagged")
        self.assertEqual(repaired_again["creation_status"], "already_tagged")
        self.assertEqual(len(client.created), 1)

    def test_failed_created_ledger_write_keeps_started_intent_protected(self):
        with tempfile.TemporaryDirectory() as tmp:
            output, source_root, candidate_id = _approved_fried_rice(Path(tmp))
            client = FakeMealie()
            service = ImportService(output, source_root=Path(source_root), client=client)
            plan = service.preview([candidate_id])
            with patch("ui_import_service.StateStore.add", side_effect=OSError("ledger unavailable")):
                outcome = service.dispatch(plan["plan_id"])
            second = service.dispatch(plan["plan_id"])

        self.assertEqual(outcome[0]["creation_status"], "uncertain")
        self.assertEqual(second[0]["creation_status"], "uncertain")
        self.assertEqual(len(client.created), 1)

    def test_reconciliation_lookup_found_links_and_absence_does_not_release_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            output, source_root, candidate_id = _approved_fried_rice(Path(tmp))
            state = __import__("import_recipes").StateStore(output / "import_state.jsonl")
            state.add(candidate_id, "UNCERTAIN", "", "scan.pdf", "Fried Rice", identity=candidate_id)
            client = FakeMealie()
            client.get_recipe = lambda slug: None
            service = ImportService(output, source_root=Path(source_root), client=client)

            absent = service.reconcile_existing(candidate_id, "missing-slug")
            client.get_recipe = lambda slug: {"id": "valid-id", "slug": slug, "name": "Fried Rice"}
            found = service.reconcile_existing(candidate_id, "existing-slug")

        self.assertEqual(absent["creation_status"], "not_proven_absent")
        self.assertEqual(found["creation_status"], "linked_existing")
        self.assertEqual(found["slug"], "existing-slug")

    def test_cli_candidate_uses_shared_claim_and_preserves_legacy_complete_flow(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = write_ui_fixture(Path(tmp))
            output = Path(fixture["output"])
            source = Path(fixture["source"])
            digest = file_digest(source)
            view = next(item for item in load_catalog(output, source.parent)
                        if item.candidate.name == "Fried Rice")
            calls = []
            service = ImportService(output)

            first = service.dispatch_cli_candidate(
                view.candidate_id, view.candidate, source, digest, view.extraction_id,
                lambda recipe: calls.append(recipe.name) or "legacy-slug",
            )
            second = service.dispatch_cli_candidate(
                view.candidate_id, view.candidate, source, digest, view.extraction_id,
                lambda recipe: calls.append(recipe.name) or "duplicate-slug",
            )

        self.assertEqual(first["status"], "created")
        self.assertEqual(second["status"], "already_done")
        self.assertEqual(calls, ["Fried Rice"])

    def test_cli_candidate_requires_exact_modern_approval_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            output, source_root, candidate_id = _approved_fried_rice(Path(tmp))
            view = next(item for item in load_catalog(output, Path(source_root))
                        if item.candidate_id == candidate_id)
            changed = view.candidate.model_copy(deep=True)
            changed.ingredients[0] = "unapproved change"
            calls = []

            outcome = ImportService(output).dispatch_cli_candidate(
                candidate_id, changed, view.source_path, view.source_hash, view.extraction_id,
                lambda recipe: calls.append(recipe.name) or "must-not-create",
            )

        self.assertEqual(outcome["status"], "not_sent")
        self.assertEqual(calls, [])

    def test_cli_candidate_imports_matching_modern_approved_revision(self):
        with tempfile.TemporaryDirectory() as tmp:
            output, source_root, candidate_id = _approved_fried_rice(Path(tmp))
            view = next(item for item in load_catalog(output, Path(source_root))
                        if item.candidate_id == candidate_id)

            outcome = ImportService(output).dispatch_cli_candidate(
                candidate_id, view.candidate, view.source_path, view.source_hash, view.extraction_id,
                lambda recipe: "approved-slug",
            )

        self.assertEqual(outcome["status"], "created")
        self.assertEqual(outcome["slug"], "approved-slug")

    def test_malformed_service_ledger_blocks_preview_without_appending(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = write_ui_fixture(Path(tmp))
            output = Path(fixture["output"])
            event_path = output / "ui_import_events.jsonl"
            event_path.write_text('{"event":"truncated"', encoding="utf-8")
            before = event_path.read_bytes()

            with self.assertRaisesRegex(RuntimeError, "history is malformed"):
                ImportService(output, Path(fixture["source"]).parent).preview(["missing"])
            self.assertEqual(event_path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
