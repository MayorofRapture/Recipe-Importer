import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from import_recipes import StateStore, candidate_identity, process_file
from recipe_quality import Diagnostic, ExtractionBatch, RecipeCandidate, Suggestion
from review_store import ReviewStore


class ReviewImportTests(unittest.TestCase):
    def test_approved_ocr_candidate_can_enter_existing_import_flow(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "fried-rice.jpg"
            source.write_bytes(b"fixture")
            cache = root / "extracted"
            cache.mkdir()
            digest = "fried-rice-hash"
            candidate = RecipeCandidate(
                name="Fried Rice", ingredients=["2 cups rice"],
                instructions=["Cook rice."], source_method="python",
                confidence="high", requires_review=True,
            )
            (cache / f"{digest}.json").write_text(
                ExtractionBatch(recipes=[candidate]).model_dump_json(), encoding="utf-8")
            reviews = ReviewStore(root / "review.jsonl")
            identity = candidate_identity(digest, candidate)
            reviews.add_candidate(str(source), identity, candidate)
            reviews.record_candidate_decision(str(source), identity, "approved")
            state = StateStore(root / "state.jsonl")
            imported = []

            with patch("import_recipes.import_mealie",
                       side_effect=lambda recipe, *_: imported.append(recipe) or "fried-rice"):
                rows = process_file(
                    source, digest,
                    {"api_key": "", "mealie_url": "http://mealie",
                     "mealie_token": "token", "model": "primary"},
                    None, state, cache, False, review_store=reviews,
                )

        self.assertEqual([row["status"] for row in rows], ["IMPORTED"])
        self.assertEqual(imported[0].instructions, ["Cook rice."])

    def test_edited_ocr_candidate_uses_only_recorded_edit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "fried-rice.jpg"
            source.write_bytes(b"fixture")
            cache = root / "extracted"
            cache.mkdir()
            digest = "fried-rice-edit-hash"
            candidate = RecipeCandidate(
                name="Fried Rice", ingredients=["rice"], instructions=["Cook."],
                source_method="gemini_text", confidence="medium", requires_review=True,
            )
            (cache / f"{digest}.json").write_text(
                ExtractionBatch(recipes=[candidate]).model_dump_json(), encoding="utf-8")
            reviews = ReviewStore(root / "review.jsonl")
            identity = candidate_identity(digest, candidate)
            reviews.add_candidate(str(source), identity, candidate)
            reviews.record_candidate_decision(
                str(source), identity, "edited",
                json.dumps({"name": "Fried Rice Deluxe", "ingredients": ["3 cups rice"],
                            "instructions": ["Cook rice slowly."]}),
            )
            state = StateStore(root / "state.jsonl")
            imported = []

            with patch("import_recipes.import_mealie",
                       side_effect=lambda recipe, *_: imported.append(recipe) or "fried-rice-deluxe"):
                rows = process_file(
                    source, digest,
                    {"api_key": "", "mealie_url": "http://mealie",
                     "mealie_token": "token", "model": "primary"},
                    None, state, cache, False, review_store=reviews,
                )

        self.assertEqual([row["status"] for row in rows], ["IMPORTED"])
        self.assertEqual(imported[0].name, "Fried Rice Deluxe")
        self.assertEqual(imported[0].ingredients, ["3 cups rice"])
        self.assertEqual(imported[0].instructions, ["Cook rice slowly."])

    def test_approved_jalapeno_instructions_reach_import_payload_as_steps(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "jalapeno.jpg"
            source.write_bytes(b"fixture")
            cache = root / "extracted"
            cache.mkdir()
            digest = "jalapeno-hash"
            candidate = RecipeCandidate(
                name="Jalapeño Peppers",
                ingredients=["1 lb jalapeño peppers", "4 oz sharp cheese, grated"],
                instructions=[],
                missing_information=[Diagnostic(
                    field_path="instructions",
                    kind="missing_from_source",
                    severity="critical",
                    detail="The source contains no instructions.",
                )],
                suggestions=[Suggestion(
                    suggestion_id="sug_instructions_1",
                    field_path="instructions",
                    proposed_value=(
                        "1. Preheat oven to 375 degrees F.\n"
                        "2. Cut peppers in half lengthwise.\n"
                        "3. Stuff peppers with cheese."
                    ),
                    rationale="Unverified completion.",
                )],
            )
            (cache / f"{digest}.json").write_text(
                ExtractionBatch(recipes=[candidate]).model_dump_json(), encoding="utf-8")
            reviews = ReviewStore(root / "review.jsonl")
            reviews.record(str(source), candidate_identity(digest, candidate), candidate,
                           "sug_instructions_1", "approved")
            state = StateStore(root / "state.jsonl")
            imported = []

            with patch("import_recipes.import_mealie",
                       side_effect=lambda recipe, *_: imported.append(recipe) or "jalapeno-peppers"):
                rows = process_file(
                    source, digest,
                    {"api_key": "", "mealie_url": "http://mealie",
                     "mealie_token": "token", "model": "primary"},
                    None, state, cache, False, review_store=reviews,
                )

        self.assertEqual([row["status"] for row in rows], ["IMPORTED"])
        self.assertEqual(imported[0].instructions, [
            "Preheat oven to 375 degrees F.",
            "Cut peppers in half lengthwise.",
            "Stuff peppers with cheese.",
        ])

    def test_force_extract_cache_is_used_for_approved_retry_without_duplicate_import(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "jalapeno.jpg"
            source.write_bytes(b"fixture")
            cache = root / "extracted"
            cache.mkdir()
            digest = "jalapeno-hash"
            legacy_recipe = RecipeCandidate(
                name="Jalapeño Peppers",
                ingredients=["1 lb jalapeño peppers"],
                instructions=[],
            )
            legacy_cache = cache / f"{digest}.json"
            legacy_cache.write_text(
                ExtractionBatch(recipes=[legacy_recipe]).model_dump_json(), encoding="utf-8")
            fresh = RecipeCandidate(
                name="Jalapeño Peppers",
                ingredients=["1 lb jalapeño peppers"],
                instructions=[],
                missing_information=[Diagnostic(
                    field_path="instructions",
                    kind="missing_from_source",
                    severity="critical",
                    detail="The source contains no instructions.",
                )],
                suggestions=[Suggestion(
                    suggestion_id="sug_instructions_1",
                    field_path="instructions",
                    proposed_value="1. Preheat oven.\n2. Bake peppers.",
                    rationale="Unverified completion.",
                )],
            )
            reviews = ReviewStore(root / "review.jsonl")
            state = StateStore(root / "state.jsonl")
            config = {"api_key": "", "mealie_url": "http://mealie",
                      "mealie_token": "token", "model": "primary"}

            with patch("import_recipes.extract", return_value=ExtractionBatch(recipes=[fresh])):
                first_rows = process_file(
                    source, digest, config, None, state, cache, False,
                    force_extract=True, review_store=reviews,
                )
            fresh_caches = list(cache.glob(f"{digest}--*.json"))
            legacy_preserved = legacy_cache.exists()
            os.utime(legacy_cache, ns=(1, 1))
            os.utime(fresh_caches[0], ns=(1, 1))

            reviews.record(str(source), candidate_identity(digest, fresh), fresh,
                           "sug_instructions_1", "approved")
            imported = []
            with patch("import_recipes.import_mealie",
                       side_effect=lambda recipe, *_: imported.append(recipe) or "jalapeno-peppers"):
                approved_rows = process_file(
                    source, digest, config, None, state, cache, False,
                    review_store=reviews,
                )
                rerun_rows = process_file(
                    source, digest, config, None, state, cache, False,
                    review_store=reviews,
                )

        self.assertEqual([row["status"] for row in first_rows], ["NEEDS_REVIEW"])
        self.assertTrue(legacy_preserved)
        self.assertEqual(len(fresh_caches), 1)
        self.assertEqual([row["status"] for row in approved_rows], ["IMPORTED"])
        self.assertEqual([row["status"] for row in rerun_rows], ["ALREADY_DONE"])
        self.assertEqual(len(imported), 1)
        self.assertEqual(imported[0].instructions, ["Preheat oven.", "Bake peppers."])


if __name__ == "__main__":
    unittest.main()
