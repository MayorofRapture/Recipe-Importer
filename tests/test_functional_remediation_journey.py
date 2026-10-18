from __future__ import annotations

import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from batch_store import BatchStore
from recipe_quality import ExtractionBatch, RecipeCandidate, schema_recipe
from review_store import ReviewStore
from ui_data import load_catalog
from ui_import_service import ImportService
from ui_job_service import JobService


def _png(color: tuple[int, int, int]) -> bytes:
    image = Image.new("RGB", (24, 24), color)
    result = io.BytesIO()
    image.save(result, format="PNG")
    return result.getvalue()


class _SyntheticMealie:
    supports_empty_instructions = True

    def __init__(self):
        self.created = []
        self.recipes = {}
        self.tags = []
        self.fail_first_tag = True

    def create_recipe(self, recipe):
        payload = schema_recipe(recipe)
        slug = f"synthetic-{len(self.created) + 1}"
        record = {"id": f"id-{slug}", "slug": slug, "name": recipe.name, **payload}
        self.created.append(payload)
        self.recipes[slug] = record
        if recipe.name == "Synthetic Chili":
            raise TimeoutError("simulated timeout after provider acceptance")
        return slug

    def get_recipe(self, slug):
        return self.recipes.get(slug)

    def ensure_tag(self, name):
        if self.fail_first_tag:
            self.fail_first_tag = False
            raise RuntimeError("synthetic first tag failure")
        return {"id": "synthetic-tag", "name": name}

    def add_tag(self, slug, tag):
        self.tags.append((slug, tag["name"]))
        return "tagged"


class FunctionalRemediationJourneyTests(unittest.TestCase):
    def test_synthetic_upload_retry_review_approval_import_and_reconciliation_journey(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "output"
            fixture_mode = patch.dict(os.environ, {"RECIPE_IMPORTER_FIXTURE_MODE": "1"})
            fixture_mode.start()
            self.addCleanup(fixture_mode.stop)
            batches = BatchStore(output)
            batch = batches.create("Synthetic winter cookbook", "Synthetic winter tag")
            source_a = batches.stage(batch["batch_id"], "recipes-a.png", _png((10, 30, 50)))
            source_b = batches.stage(batch["batch_id"], "recipes-b.png", _png((70, 90, 110)))

            rice = RecipeCandidate(
                name="Synthetic Rice", ingredients=["2 cups rice", "1 egg"],
                instructions=["Cook rice.", "Fold in egg."],
            )
            chili = RecipeCandidate(
                name="Synthetic Chili", ingredients=["2 cups beans", "1 cup tomatoes"],
                instructions=["Simmer beans and tomatoes."],
            )
            seasoning = RecipeCandidate(
                name="Synthetic Seasoning", ingredients=["1 tsp oregano", "1 tsp thyme"],
                instructions=[],
            )
            calls: list[tuple[str, int]] = []
            by_source: dict[str, int] = {}

            def extractor(source, _mode, _config):
                filename = source["original_filename"]
                by_source[filename] = by_source.get(filename, 0) + 1
                calls.append((filename, by_source[filename]))
                if filename == "recipes-a.png":
                    return ExtractionBatch(recipes=[rice, chili])
                if by_source[filename] == 1:
                    raise RuntimeError("synthetic interrupted extraction")
                return ExtractionBatch(recipes=[seasoning])

            jobs = JobService(output, extractor=extractor)
            started = jobs.start(batch["batch_id"], "local_ocr")
            partial = jobs.run_pending(started["job_id"])
            self.assertEqual(partial["status"], "completed_with_errors")
            self.assertEqual(partial["failure_count"], 1)
            resumed = jobs.resume(started["job_id"])
            completed = jobs.run_pending(started["job_id"])
            self.assertEqual(resumed["status"], "queued")
            self.assertEqual(completed["status"], "completed")
            self.assertEqual(by_source, {"recipes-a.png": 1, "recipes-b.png": 2})
            self.assertEqual(len(load_catalog(output)), 3)

            # The second file is deliberately unavailable for manual source-unavailable review.
            Path(source_b["staged_path"]).unlink()
            views = load_catalog(output)
            view_by_name = {view.candidate.name: view for view in views}
            reviews = ReviewStore(output / "review_events.jsonl")
            approved_ids = []
            for name, operation in (("Synthetic Rice", "journey-rice"),
                                     ("Synthetic Chili", "journey-chili"),
                                     ("Synthetic Seasoning", "journey-seasoning")):
                view = view_by_name[name]
                recipe = view.candidate.model_copy(deep=True)
                if name == "Synthetic Rice":
                    recipe.yield_text = "4 servings"
                saved = reviews.save_revision(
                    candidate_id=view.candidate_id, source_hash=view.source_hash,
                    extraction_id=view.extraction_id, expected_parent=0,
                    operation_id=f"{operation}:save", recipe=recipe.model_dump(mode="json"),
                    target_bindings=list(view.target_bindings),
                )
                if name == "Synthetic Seasoning":
                    reviews.record_no_instructions_decision(
                        candidate_id=view.candidate_id, source_hash=view.source_hash,
                        extraction_id=view.extraction_id, revision=saved["revision"],
                        reason="Dry seasoning blend", operation_id=f"{operation}:no-instructions",
                    )
                reviews.approve_revision(
                    candidate_id=view.candidate_id, source_hash=view.source_hash,
                    extraction_id=view.extraction_id, revision=saved["revision"],
                    operation_id=f"{operation}:approval", source_path=view.source_path,
                    approval_method="manual_source_unavailable", reviewer_acknowledged=True,
                    review_rationale="Reviewed the exact saved recipe; original scan is unavailable.",
                )
                approved_ids.append(view.candidate_id)

            client = _SyntheticMealie()
            imports = ImportService(output, source_root=output / "ui_batches", client=client)
            plan = imports.preview(approved_ids, batch_tag="Synthetic winter tag")
            self.assertEqual(len(plan["eligible"]), 3)
            rice_payload = next(item["recipe"] for item in plan["eligible"]
                                if item["title"] == "Synthetic Rice")
            seasoning_payload = next(item["recipe"] for item in plan["eligible"]
                                     if item["title"] == "Synthetic Seasoning")
            self.assertEqual(rice_payload["yield_text"], "4 servings")
            self.assertEqual(seasoning_payload["instructions"], [])

            outcomes = imports.dispatch(plan["plan_id"])
            by_name = {item["title"]: item for item in outcomes}
            self.assertEqual(by_name["Synthetic Rice"]["tag_status"], "failed")
            self.assertEqual(by_name["Synthetic Chili"]["creation_status"], "uncertain")
            self.assertEqual(by_name["Synthetic Seasoning"]["creation_status"], "created")
            self.assertEqual(client.created[0]["recipeYield"], "4 servings")
            self.assertEqual(client.created[2]["recipeInstructions"], [])

            client.fail_first_tag = False
            repaired_tag = imports.repair_tag(view_by_name["Synthetic Rice"].candidate_id)
            self.assertEqual(repaired_tag["tag_status"], "tagged")
            uncertain_events = [event for event in imports._read_events()
                                if event.get("event") == "intent_started"
                                and event.get("candidate_id") == view_by_name["Synthetic Chili"].candidate_id]
            attempt_id = uncertain_events[0]["attempt_id"]
            chili_slug = "synthetic-2"
            inspection = imports.reconcile_existing(
                view_by_name["Synthetic Chili"].candidate_id, chili_slug, attempt_id)
            self.assertEqual(inspection["creation_status"], "confirmation_required")
            linked = imports.reconcile_existing(
                view_by_name["Synthetic Chili"].candidate_id, chili_slug, attempt_id,
                reviewer_confirmed=True,
                evidence="Compared ingredients and instructions with the exact approved revision.",
                unavailable_field_ack=True,
                observed_recipe_digest=inspection["comparison_digest"],
            )
            self.assertEqual(linked["creation_status"], "linked_existing")
            self.assertEqual(imports.dispatch(plan["plan_id"])[0]["creation_status"], "already_created")
            self.assertEqual(len(client.created), 3)
            self.assertIn(("synthetic-1", "Synthetic winter tag"), client.tags)


if __name__ == "__main__":
    unittest.main()
