from __future__ import annotations

import tempfile
import unittest
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from import_recipes import file_digest, repair_import_tags
from recipe_quality import ExtractionBatch, RecipeCandidate, Suggestion, schema_recipe
from review_store import ReviewStore, _recipe_digest, extraction_identity
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


class SimulatedProcessStop(BaseException):
    """Model abrupt process death without being caught as a provider error."""


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
    def test_shared_attempt_snapshot_policy_ignores_review_metadata_but_rejects_content_or_payload_changes(self):
        approved = RecipeCandidate(
            name="OCR Bread", ingredients=["2 cups flour"], instructions=["Bake."],
            requires_review=False,
        ).model_dump(mode="json")
        saved_revision = dict(approved, requires_review=True)
        digest = _recipe_digest(approved)
        intent = {
            "title": approved["name"], "content_sha256": digest,
            "attempt_evidence_version": 2, "recipe_snapshot": approved,
            "outbound_payload": schema_recipe(RecipeCandidate.model_validate(approved)),
        }

        accepted, reason = ImportService._validated_attempt_snapshot(
            intent, {"recipe": saved_revision, "content_sha256": digest},
        )
        changed = dict(approved, ingredients=["3 cups flour"])
        bad_content, _ = ImportService._validated_attempt_snapshot(
            dict(intent, recipe_snapshot=changed,
                 outbound_payload=schema_recipe(RecipeCandidate.model_validate(changed))),
            {"recipe": saved_revision, "content_sha256": digest},
        )
        bad_payload, _ = ImportService._validated_attempt_snapshot(
            dict(intent, outbound_payload={"name": "Changed payload"}),
            {"recipe": saved_revision, "content_sha256": digest},
        )

        self.assertEqual(accepted, approved, reason)
        self.assertIsNone(bad_content)
        self.assertIsNone(bad_payload)

    def test_reconciliation_comparison_covers_jsonld_fields_and_embedded_notes(self):
        candidate = RecipeCandidate(
            name="Toast", ingredients=["2 slices bread"], instructions=["Toast until golden."],
            yield_text="1 serving", description="Use sturdy bread.", notes=["Butter is optional."],
        )
        provider = schema_recipe(candidate)
        provider.update({"id": "toast-id", "slug": "toast"})
        comparison = ImportService._compare_reconciliation_recipe(
            candidate.model_dump(mode="json"), provider,
        )

        self.assertEqual(comparison["differences"], [])
        self.assertEqual(comparison["material_differences"], [])
        self.assertEqual(comparison["local"]["yield_text"], "1 serving")
        self.assertEqual(comparison["mealie"]["yield_text"], "1 serving")
        self.assertIn("Recipe note: Butter is optional.", comparison["mealie"]["description"])
        self.assertEqual(comparison["local"]["notes"], ["Butter is optional."])
        self.assertEqual(comparison["unavailable_fields"], ["notes"])
        self.assertFalse(comparison["complete_evidence"])

    def test_reconciliation_comparison_marks_yield_description_and_notes_differences(self):
        candidate = RecipeCandidate(
            name="Soup", ingredients=["beans"], instructions=["Simmer."],
            yield_text="4 servings", description="With herbs.", notes=["Salt to taste."],
        )
        provider = schema_recipe(candidate)
        provider["recipeYield"] = "6 servings"
        provider["description"] = "With different herbs.\n\nRecipe note: Add pepper."
        provider["notes"] = ["Add pepper."]
        comparison = ImportService._compare_reconciliation_recipe(
            candidate.model_dump(mode="json"), provider,
        )

        self.assertEqual(set(comparison["differences"]), {"yield_text", "description", "notes"})
        self.assertEqual(set(comparison["material_differences"]), {"yield_text", "description", "notes"})
        self.assertEqual(comparison["unavailable_fields"], [])

    def test_reconciliation_comparison_ignores_formatting_only_differences(self):
        candidate = RecipeCandidate(
            name="Bean Soup", ingredients=["2 cups beans"], instructions=["Simmer gently."],
            yield_text="4 servings", description="Serve warm.", notes=["Add salt to taste."],
        )
        provider = schema_recipe(candidate)
        provider.update({
            "name": "  BEAN   SOUP ", "recipeIngredient": [" 2 CUPS beans "],
            "recipeInstructions": [{"@type": "HowToStep", "text": " simmer gently.  "}],
            "recipeYield": " 4   servings ",
            "description": " serve warm.  Recipe note:   add salt to taste. ",
            "notes": [" ADD SALT TO TASTE. "],
        })

        comparison = ImportService._compare_reconciliation_recipe(
            candidate.model_dump(mode="json"), provider,
        )

        self.assertEqual(comparison["differences"], [])
        self.assertFalse(comparison["unavailable_fields"])
        self.assertTrue(comparison["is_match"])

    def test_reconciliation_comparison_does_not_claim_match_when_provider_fields_are_absent(self):
        candidate = RecipeCandidate(
            name="Toast", ingredients=["bread"], instructions=["Toast."],
            yield_text="2 servings", description="Serve warm.", notes=["Add jam."],
        )
        provider = {"id": "toast-id", "slug": "toast", "name": "Toast",
                    "recipeIngredient": ["bread"], "recipeInstructions": ["Toast."]}
        comparison = ImportService._compare_reconciliation_recipe(
            candidate.model_dump(mode="json"), provider,
        )

        self.assertEqual(set(comparison["unavailable_fields"]), {"yield_text", "description", "notes"})
        self.assertFalse(comparison["complete_evidence"])
        self.assertFalse(comparison["is_match"])

    def test_reconciliation_digest_binds_yield_description_notes_and_evidence_version(self):
        base = {"id": "id", "slug": "slug", "name": "Recipe",
                "recipeIngredient": ["1 cup flour"], "recipeInstructions": ["Mix."],
                "recipeYield": "4", "description": "Simple."}
        changed = dict(base, recipeYield="6")

        self.assertNotEqual(ImportService._reconciliation_digest(base),
                            ImportService._reconciliation_digest(changed))

    def test_reconciliation_rejects_changed_provider_evidence_after_inspection(self):
        with tempfile.TemporaryDirectory() as tmp:
            output, source_root, candidate_id = _approved_fried_rice(Path(tmp))
            response = {"id": "same-recipe-id", "slug": "existing-rice", "name": "Fried Rice",
                        "recipeIngredient": ["3 cups cooked rice", "1 egg"],
                        "recipeInstructions": [{"text": "Cook rice."}, {"text": "Fold in egg."}],
                        "recipeYield": "4 servings", "description": "Weeknight fried rice.",
                        "notes": []}
            client = FakeMealie(create_error=TimeoutError("synthetic timeout"))
            client.get_recipe = lambda _slug: dict(response)
            service = ImportService(output, source_root=Path(source_root), client=client)
            plan = service.preview([candidate_id])
            service.dispatch(plan["plan_id"])
            attempt_id = next(item["attempt_id"] for item in service._read_events()
                              if item.get("event") == "intent_started")
            inspected = service.reconcile_existing(candidate_id, "existing-rice", attempt_id)
            response["recipeYield"] = "6 servings"
            rejected = service.reconcile_existing(
                candidate_id, "existing-rice", attempt_id, reviewer_confirmed=True,
                evidence="Compared this specific recipe and acknowledged changed provider content.",
                material_difference_ack=True, unavailable_field_ack=True,
                observed_recipe_digest=inspected["comparison_digest"],
            )
            reconciled = [item for item in service._read_events()
                          if item.get("event") == "reconciled_existing"]

        self.assertEqual(rejected["creation_status"], "not_verified")
        self.assertIn("changed after it was displayed", rejected["reason"])
        self.assertEqual(reconciled, [])

    def test_reconciliation_feedback_survives_restart_and_dismissal_is_durable(self):
        with tempfile.TemporaryDirectory() as tmp:
            output, source_root, candidate_id = _approved_fried_rice(Path(tmp))
            client = FakeMealie(create_error=TimeoutError("synthetic timeout"))
            service = ImportService(output, source_root=Path(source_root), client=client)
            plan = service.preview([candidate_id])
            service.dispatch(plan["plan_id"])
            service.begin_reconciliation_action(
                candidate_id=candidate_id, action_id="ui-action-stable-id",
                action="Inspect existing Mealie recipe",
            )
            in_progress = next(item for item in ImportService(
                output, source_root=Path(source_root), client=client,
            ).history() if item.get("candidate_id") == candidate_id)
            service.record_reconciliation_feedback(
                candidate_id=candidate_id, action_id="ui-action-stable-id",
                action="Inspect existing Mealie recipe", result_status="not_verified",
                reason="The selected recipe could not be verified.",
                next_step="Keep this recipe protected and inspect the Mealie identity.",
                protection_active=True,
            )
            restarted = ImportService(output, source_root=Path(source_root), client=client)
            history = next(item for item in restarted.history()
                           if item.get("candidate_id") == candidate_id)
            restarted.dismiss_reconciliation_feedback(candidate_id, "ui-action-stable-id")
            after_dismiss = next(item for item in ImportService(
                output, source_root=Path(source_root), client=client,
            ).history() if item.get("candidate_id") == candidate_id)

        self.assertEqual(history["creation_status"], "uncertain")
        self.assertEqual(in_progress["reconciliation_feedback"]["result_status"], "in_progress")
        self.assertEqual(history["reconciliation_feedback"]["action_id"], "ui-action-stable-id")
        self.assertEqual(history["reconciliation_feedback"]["result_status"], "not_verified")
        self.assertTrue(history["reconciliation_feedback"]["protection_active"])
        self.assertIn("keep this recipe protected", history["reconciliation_feedback"]["next_step"].casefold())
        self.assertNotIn("reconciliation_feedback", after_dismiss)

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
        with tempfile.TemporaryDirectory() as tmp:
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
        with tempfile.TemporaryDirectory() as tmp:
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
            project_root = Path(__file__).resolve().parents[1]
            first = subprocess.Popen(args, cwd=project_root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            second = subprocess.Popen(args, cwd=project_root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
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

    def test_uncertain_attempt_reconciles_without_compatibility_state_and_requires_human_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            output, source_root, candidate_id = _approved_fried_rice(Path(tmp))
            client = FakeMealie(create_error=TimeoutError("synthetic timeout after possible create"))
            service = ImportService(output, source_root=Path(source_root), client=client)
            plan = service.preview([candidate_id])
            uncertain = service.dispatch(plan["plan_id"])
            self.assertEqual(uncertain[0]["creation_status"], "uncertain")
            self.assertIsNone(__import__("import_recipes").StateStore(
                output / "import_state.jsonl").records.get(candidate_id))
            events = [json.loads(line) for line in (output / "ui_import_events.jsonl").read_text(encoding="utf-8").splitlines()]
            intent = next(item for item in events if item.get("event") == "intent_started")
            client.get_recipe = lambda slug: None

            absent = service.reconcile_existing(candidate_id, "missing-slug", intent["attempt_id"])
            client.get_recipe = lambda slug: {"id": "valid-id", "slug": slug, "name": "Fried Rice"}
            inspected = service.reconcile_existing(candidate_id, "existing-slug", intent["attempt_id"])
            self.assertEqual(inspected["creation_status"], "confirmation_required", inspected.get("reason"))
            self.assertIn("ingredients", inspected["comparison"]["unavailable_fields"])
            self.assertFalse(inspected["comparison"]["is_match"])
            blocked = service.reconcile_existing(
                candidate_id, "existing-slug", intent["attempt_id"], reviewer_confirmed=True,
                evidence="The title appears familiar.", observed_recipe_digest=inspected["comparison_digest"])
            found = service.reconcile_existing(
                candidate_id, "existing-slug", intent["attempt_id"], reviewer_confirmed=True,
                evidence="Compared the returned ingredients and steps with the approved review revision.",
                material_difference_ack=True, unavailable_field_ack=True,
                observed_recipe_digest=inspected["comparison_digest"])
            repeated = service.reconcile_existing(
                candidate_id, "existing-slug", intent["attempt_id"], reviewer_confirmed=True,
                evidence="Repeat confirmation.", material_difference_ack=True,
                unavailable_field_ack=True,
                observed_recipe_digest=inspected["comparison_digest"])
            second_dispatch = service.dispatch(plan["plan_id"])
            events = [json.loads(line) for line in (output / "ui_import_events.jsonl").read_text(encoding="utf-8").splitlines()]
            linked = [item for item in events if item.get("event") == "reconciled_existing"]

        self.assertEqual(absent["creation_status"], "not_proven_absent")
        self.assertEqual(blocked["creation_status"], "not_verified")
        self.assertEqual(found["creation_status"], "linked_existing")
        self.assertEqual(found["slug"], "existing-slug")
        self.assertEqual(repeated["creation_status"], "already_linked")
        self.assertIn(second_dispatch[0]["creation_status"], {"already_created", "protected", "uncertain"})
        self.assertEqual(len(client.created), 1)
        self.assertEqual(len(linked), 1)
        self.assertEqual(linked[0]["attempt_id"], intent["attempt_id"])
        self.assertIn("approved review revision", linked[0]["reviewer_evidence"])

    def test_reconciled_recipe_remains_tag_recoverable_when_compatibility_write_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            output, source_root, candidate_id = _approved_fried_rice(Path(tmp))
            client = FakeMealie(create_error=TimeoutError("synthetic timeout"))
            service = ImportService(output, source_root=Path(source_root), client=client)
            plan = service.preview([candidate_id], batch_tag="Original batch")
            service.dispatch(plan["plan_id"])
            intent = next(item for item in service._read_events() if item.get("event") == "intent_started")
            snapshot = intent["recipe_snapshot"]
            client.get_recipe = lambda slug: {
                "id": "existing-id", "slug": slug, "name": snapshot["name"],
                "recipeIngredient": snapshot["ingredients"],
                "recipeInstructions": snapshot["instructions"],
                "recipeYield": snapshot.get("yield_text", ""),
                "description": snapshot.get("description", ""), "notes": snapshot.get("notes", []),
            }
            inspected = service.reconcile_existing(candidate_id, "existing-slug", intent["attempt_id"])
            with patch("ui_import_service.StateStore.add", side_effect=OSError("ledger unavailable")):
                linked = service.reconcile_existing(
                    candidate_id, "existing-slug", intent["attempt_id"], reviewer_confirmed=True,
                    evidence="Compared the specific Mealie recipe with the approved review.",
                    unavailable_field_ack=True, observed_recipe_digest=inspected["comparison_digest"],
                )
            restarted = ImportService(output, source_root=Path(source_root), client=client)
            row = next(item for item in restarted.history() if item["candidate_id"] == candidate_id)
            repaired = repair_import_tags(output, "Original batch", client)[0]
            repeated = ImportService(output, client=client).repair_tag(candidate_id)
            events = restarted._read_events()

        self.assertEqual(linked["creation_status"], "linked_existing", linked.get("reason"))
        self.assertEqual(row["tag_status"], "pending")
        self.assertEqual(repaired["tag_status"], "tagged")
        self.assertEqual(repeated["creation_status"], "already_tagged")
        self.assertEqual(repeated["tag_status"], "tagged")
        self.assertEqual(len(client.created), 1)
        self.assertEqual(len([item for item in events if item.get("event") == "reconciled_existing"]), 1)

    def test_historical_cli_attempt_without_snapshot_requires_human_reconciliation(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = write_ui_fixture(Path(tmp))
            output, source = Path(fixture["output"]), Path(fixture["source"])
            view = next(item for item in load_catalog(output, source.parent)
                        if item.candidate.name == "Fried Rice")
            events_path = output / "ui_import_events.jsonl"
            attempt_id = "historical-cli-attempt"
            historical_intent = {
                "event": "intent_started", "schema_version": 1,
                "attempt_id": attempt_id, "plan_id": "cli",
                "candidate_id": view.candidate_id, "source_hash": view.source_hash,
                "extraction_id": view.extraction_id, "revision": view.revision,
                "title": view.candidate.name, "source_path": str(source),
                "batch_tag": "Historical batch", "created_at": "2024-01-01T00:00:00+00:00",
            }
            with events_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(historical_intent) + "\n")
                fh.write(json.dumps({"event": "create_uncertain", "attempt_id": attempt_id,
                                     "candidate_id": view.candidate_id, "created_at": "2024-01-01T00:00:01+00:00"}) + "\n")
            client = FakeMealie()
            client.get_recipe = lambda slug: {
                "id": "historical-mealie-id", "slug": slug, "name": "Fried Rice",
                "recipeIngredient": ["3 cups cooked rice", "1 egg"],
                "recipeInstructions": ["Cook rice.", "Fold in egg."],
            }
            service = ImportService(output, source_root=source.parent, client=client)
            inspected = service.reconcile_existing(view.candidate_id, "old-slug", attempt_id)
            evidence_missing = service.reconcile_existing(
                view.candidate_id, "old-slug", attempt_id, reviewer_confirmed=True,
                evidence="The title is familiar.", unavailable_field_ack=True,
                observed_recipe_digest=inspected.get("comparison_digest", ""),
            )
            linked = service.reconcile_existing(
                view.candidate_id, "old-slug", attempt_id, reviewer_confirmed=True,
                evidence="Matched the source filename, date, and ingredient list in my archived cooking notes.",
                material_difference_ack=True, unavailable_field_ack=True, historical_content_ack=True,
                observed_recipe_digest=inspected.get("comparison_digest", ""),
            )
            repeated = ImportService(output, client=client).reconcile_existing(
                view.candidate_id, "different-slug", attempt_id, reviewer_confirmed=True,
                evidence="Repeated verification.", historical_content_ack=True,
                unavailable_field_ack=True,
                observed_recipe_digest=inspected.get("comparison_digest", ""),
            )
            final_events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]

        self.assertEqual(inspected["creation_status"], "confirmation_required", inspected.get("reason"))
        self.assertTrue(inspected["comparison"]["unavailable_fields"])
        self.assertEqual(evidence_missing["creation_status"], "not_verified")
        self.assertEqual(linked["creation_status"], "linked_existing", linked.get("reason"))
        self.assertEqual(repeated["creation_status"], "already_linked")
        reconciliations = [item for item in final_events if item.get("event") == "reconciled_existing"]
        self.assertEqual(len(reconciliations), 1)
        self.assertEqual(reconciliations[0]["attempt_id"], attempt_id)
        self.assertEqual(reconciliations[0]["mealie_recipe_id"], "historical-mealie-id")
        self.assertTrue(reconciliations[0]["historical_content_ack"])

    def test_orphaned_create_intent_is_inspectable_and_reconcilable_without_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            output, source_root, candidate_id = _approved_fried_rice(Path(tmp))
            client = FakeMealie()
            service = ImportService(output, source_root=Path(source_root), client=client)
            plan = service.preview([candidate_id])
            create_calls = []

            def stop_before_create(_recipe):
                create_calls.append("attempted")
                raise SimulatedProcessStop("process terminated after durable intent")

            client.create_recipe = stop_before_create
            with self.assertRaises(SimulatedProcessStop):
                service.dispatch(plan["plan_id"])
            events = [json.loads(line) for line in
                      (output / "ui_import_events.jsonl").read_text(encoding="utf-8").splitlines()]
            intent = next(item for item in events if item.get("event") == "intent_started")

            restarted = ImportService(output, source_root=Path(source_root), client=client)
            history = next(item for item in restarted.history()
                           if item.get("candidate_id") == candidate_id)
            client.get_recipe = lambda _slug: None
            absent = restarted.reconcile_existing(candidate_id, "missing-slug", intent["attempt_id"])
            retry_blocked = restarted.dispatch(plan["plan_id"])
            snapshot = intent["recipe_snapshot"]
            client.get_recipe = lambda slug: {
                "id": "existing-recipe-id", "slug": slug, "name": snapshot["name"],
                "recipeIngredient": snapshot["ingredients"],
                "recipeInstructions": snapshot["instructions"],
                "recipeYield": snapshot.get("yield_text", ""),
                "description": snapshot.get("description", ""),
            }
            inspection = restarted.reconcile_existing(candidate_id, "existing-slug", intent["attempt_id"])
            linked = restarted.reconcile_existing(
                candidate_id, "existing-slug", intent["attempt_id"], reviewer_confirmed=True,
                evidence="Compared the selected Mealie recipe with the stored approved snapshot.",
                unavailable_field_ack=True,
                observed_recipe_digest=inspection["comparison_digest"],
            )
            repeated = ImportService(output, source_root=Path(source_root), client=client).reconcile_existing(
                candidate_id, "existing-slug", intent["attempt_id"], reviewer_confirmed=True,
                evidence="Repeated durable reconciliation.",
                unavailable_field_ack=True,
                observed_recipe_digest=inspection["comparison_digest"],
            )
            final_history = next(item for item in restarted.history()
                                 if item.get("candidate_id") == candidate_id)

        self.assertEqual(inspection["creation_status"], "confirmation_required")
        self.assertEqual(inspection["attempt_id"], intent["attempt_id"])
        self.assertEqual(linked["creation_status"], "linked_existing")
        self.assertEqual(repeated["creation_status"], "already_linked")
        self.assertEqual(final_history["attempt_status"], "reconciled")
        self.assertEqual(history.get("attempt_id"), intent["attempt_id"])
        self.assertEqual(history.get("attempt_status"), "orphaned")
        self.assertEqual(absent["creation_status"], "not_proven_absent")
        self.assertEqual(retry_blocked[0]["creation_status"], "uncertain")
        self.assertEqual(len(client.created), 0)
        self.assertEqual(len(create_calls), 1)

    def test_ocr_review_metadata_difference_does_not_block_uncertain_reconciliation(self):
        with tempfile.TemporaryDirectory() as tmp:
            output, source_root, candidate_id = _approved_fried_rice(Path(tmp))
            reviews = ReviewStore(output / "review_events.jsonl")
            view = next(item for item in load_catalog(output, Path(source_root))
                        if item.candidate_id == candidate_id)
            ocr_recipe = view.candidate.model_copy(update={
                "source_method": "ocr", "requires_review": True,
            })
            saved = reviews.save_revision(
                candidate_id=candidate_id, source_hash=view.source_hash,
                extraction_id=view.extraction_id, expected_parent=view.revision,
                operation_id="ocr-review-metadata-revision",
                recipe=ocr_recipe.model_dump(mode="json"),
                target_bindings=list(view.target_bindings),
            )
            reviews.approve_revision(
                candidate_id=candidate_id, source_hash=view.source_hash,
                extraction_id=view.extraction_id, revision=saved["revision"],
                operation_id="ocr-review-metadata-approval", source_path=view.source_path,
            )
            client = FakeMealie(create_error=TimeoutError("synthetic timeout"))
            service = ImportService(output, source_root=Path(source_root), client=client)
            plan = service.preview([candidate_id])
            dispatched = service.dispatch(plan["plan_id"])
            intent = next(item for item in service._read_events()
                          if item.get("event") == "intent_started")
            effective = intent["recipe_snapshot"]
            self.assertFalse(effective["requires_review"])
            self.assertTrue(saved["recipe"]["requires_review"])
            client.get_recipe = lambda slug: {
                "id": "ocr-existing", "slug": slug, "name": effective["name"],
                "recipeIngredient": effective["ingredients"],
                "recipeInstructions": effective["instructions"],
                "recipeYield": effective.get("yield_text", ""),
                "description": effective.get("description", ""),
            }

            inspected = service.reconcile_existing(candidate_id, "ocr-existing", intent["attempt_id"])

        self.assertEqual(dispatched[0]["creation_status"], "uncertain")
        self.assertEqual(inspected["creation_status"], "confirmation_required", inspected.get("reason"))

    def test_legacy_cli_uncertain_intent_has_reconcilable_content_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = write_ui_fixture(Path(tmp))
            output, source = Path(fixture["output"]), Path(fixture["source"])
            digest = file_digest(source)
            view = next(item for item in load_catalog(output, source.parent)
                        if item.candidate.name == "Fried Rice")
            candidate = view.candidate
            service = ImportService(output, source_root=source.parent,
                                    client=FakeMealie(create_error=TimeoutError("synthetic timeout")))
            result = service.dispatch_cli_candidate(
                view.candidate_id, candidate, source, digest, view.extraction_id,
                lambda _recipe: (_ for _ in ()).throw(TimeoutError("synthetic timeout")),
            )
            intent = next(item for item in service._read_events()
                          if item.get("event") == "intent_started")
            snapshot = intent["recipe_snapshot"]
            service.client.get_recipe = lambda slug: {
                "id": "legacy-existing", "slug": slug, "name": snapshot["name"],
                "recipeIngredient": snapshot["ingredients"],
                "recipeInstructions": snapshot["instructions"],
            }

            inspected = service.reconcile_existing(view.candidate_id, "legacy-existing", intent["attempt_id"])
            linked = service.reconcile_existing(
                view.candidate_id, "legacy-existing", intent["attempt_id"],
                reviewer_confirmed=True,
                evidence="Compared the selected Mealie recipe with the durable legacy create snapshot.",
                material_difference_ack=True, unavailable_field_ack=True,
                observed_recipe_digest=inspected["comparison_digest"],
            )
            create_calls = []
            second = service.dispatch_cli_candidate(
                view.candidate_id, candidate, source, digest, view.extraction_id,
                lambda _recipe: create_calls.append("called") or "duplicate-slug",
            )

        self.assertEqual(result["status"], "uncertain")
        self.assertEqual(intent["authorization_mode"], "legacy_auto_import")
        self.assertEqual(intent["attempt_evidence_version"], 2)
        self.assertEqual(inspected["creation_status"], "confirmation_required", inspected.get("reason"))
        self.assertEqual(linked["creation_status"], "linked_existing")
        self.assertIn(second["status"], {"already_done", "uncertain"}, second.get("reason"))
        self.assertEqual(create_calls, [])


    def test_reconciliation_fails_closed_on_missing_attempt_or_mismatched_returned_slug(self):
        with tempfile.TemporaryDirectory() as tmp:
            output, source_root, candidate_id = _approved_fried_rice(Path(tmp))
            client = FakeMealie(create_error=TimeoutError("synthetic timeout"))
            service = ImportService(output, source_root=Path(source_root), client=client)
            plan = service.preview([candidate_id])
            service.dispatch(plan["plan_id"])
            missing = service.reconcile_existing(candidate_id, "some-slug", "missing-attempt")
            events = [json.loads(line) for line in (output / "ui_import_events.jsonl").read_text(encoding="utf-8").splitlines()]
            attempt_id = next(item["attempt_id"] for item in events if item.get("event") == "intent_started")
            client.get_recipe = lambda slug: {"id": "valid-id", "slug": "other-slug", "name": "Fried Rice"}
            wrong = service.reconcile_existing(candidate_id, "some-slug", attempt_id)

        self.assertEqual(missing["creation_status"], "not_verified")
        self.assertEqual(wrong["creation_status"], "not_verified")
        self.assertEqual(len(client.created), 1)

    def test_reconciliation_fails_closed_on_duplicate_attempt_intent(self):
        with tempfile.TemporaryDirectory() as tmp:
            output, source_root, candidate_id = _approved_fried_rice(Path(tmp))
            client = FakeMealie(create_error=TimeoutError("synthetic timeout"))
            service = ImportService(output, source_root=Path(source_root), client=client)
            plan = service.preview([candidate_id])
            service.dispatch(plan["plan_id"])
            path = output / "ui_import_events.jsonl"
            events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            intent = next(item for item in events if item.get("event") == "intent_started")
            duplicate = dict(intent)
            duplicate["event_id"] = "synthetic-duplicate-intent"
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(duplicate) + "\n")

            result = service.reconcile_existing(candidate_id, "existing-slug", intent["attempt_id"])

        self.assertEqual(result["creation_status"], "not_verified")
        self.assertIn("duplicate", result["reason"])
        self.assertEqual(len(client.created), 1)

    def test_explicit_attempt_selection_still_blocks_multiple_unresolved_intents(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "output"
            output.mkdir()
            service = ImportService(output)
            events = [{
                "event": "intent_started", "attempt_id": f"unresolved-{index}",
                "candidate_id": "ambiguous-candidate",
            } for index in range(2)]

            projection, reason = service._uncertain_attempt(events, "ambiguous-candidate", "unresolved-0")

        self.assertIsNone(projection)
        self.assertIn("multiple", reason)

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
