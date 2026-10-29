import csv
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from import_recipes import StateStore, candidate_identity, process_file
from recipe_quality import Diagnostic, ExtractionBatch, RecipeCandidate, Suggestion
from review_store import ReviewStore, _candidate_payload, _canonical, extraction_identity


class ReviewImportTests(unittest.TestCase):
    def test_review_only_mode_does_not_import_an_unreviewed_complete_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "toast.jpg"
            source.write_bytes(b"synthetic toast")
            digest = __import__("hashlib").sha256(source.read_bytes()).hexdigest()
            cache = root / "extracted"
            cache.mkdir()
            candidate = RecipeCandidate(name="Toast", ingredients=["bread"], instructions=["Toast."])
            (cache / f"{digest}.json").write_text(
                ExtractionBatch(recipes=[candidate]).model_dump_json(), encoding="utf-8")
            imported = []

            with patch("import_recipes.import_mealie",
                       side_effect=lambda recipe, *_: imported.append(recipe) or "toast"):
                rows = process_file(
                    source, digest, {"api_key": "", "mealie_url": "", "mealie_token": "",
                                    "model": "primary"}, None, StateStore(root / "state.jsonl"),
                    cache, False, legacy_auto_import=False,
                )

        self.assertEqual([row["status"] for row in rows], ["EXTRACTED_ONLY"])
        self.assertIn("--legacy-auto-import", rows[0]["note"])
        self.assertEqual(imported, [])

    def test_explicit_legacy_auto_import_keeps_legacy_create_behavior(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "toast.jpg"
            source.write_bytes(b"synthetic toast")
            digest = __import__("hashlib").sha256(source.read_bytes()).hexdigest()
            cache = root / "extracted"
            cache.mkdir()
            candidate = RecipeCandidate(name="Toast", ingredients=["bread"], instructions=["Toast."])
            (cache / f"{digest}.json").write_text(
                ExtractionBatch(recipes=[candidate]).model_dump_json(), encoding="utf-8")
            imported = []

            with patch("import_recipes.import_mealie",
                       side_effect=lambda recipe, *_: imported.append(recipe) or "toast"):
                rows = process_file(
                    source, digest, {"api_key": "", "mealie_url": "http://mealie",
                                    "mealie_token": "token", "model": "primary"}, None,
                    StateStore(root / "state.jsonl"), cache, False,
                    legacy_auto_import=True,
                )

        self.assertEqual([row["status"] for row in rows], ["IMPORTED"])
        self.assertEqual([recipe.name for recipe in imported], ["Toast"])

    def test_exact_approved_revision_remains_importable_without_legacy_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "toast.jpg"
            source.write_bytes(b"synthetic toast")
            digest = __import__("hashlib").sha256(source.read_bytes()).hexdigest()
            cache = root / "extracted"
            cache.mkdir()
            candidate = RecipeCandidate(name="Toast", ingredients=["bread"], instructions=["Toast."])
            batch = ExtractionBatch(recipes=[candidate])
            (cache / f"{digest}.json").write_text(batch.model_dump_json(), encoding="utf-8")
            reviews = ReviewStore(root / "review.jsonl")
            registration = reviews.register_candidates(
                str(source), digest,
                __import__("review_store").extraction_identity(digest, batch.model_dump(mode="json")),
                batch.recipes,
            )[0]
            revision = reviews.save_revision(
                candidate_id=registration["candidate_id"], source_hash=digest,
                extraction_id=registration["extraction_id"], expected_parent=0,
                operation_id="approved-review-revision", recipe=candidate.model_dump(mode="json"),
                target_bindings=registration["target_bindings"],
            )
            reviews.approve_revision(
                candidate_id=registration["candidate_id"], source_hash=digest,
                extraction_id=registration["extraction_id"], revision=revision["revision"],
                operation_id="approved-review-revision-approval", source_path=str(source),
            )
            imported = []
            state = StateStore(root / "state.jsonl")

            with patch("import_recipes.import_mealie",
                       side_effect=lambda recipe, *_: imported.append(recipe) or "toast"):
                rows = process_file(
                    source, digest, {"api_key": "", "mealie_url": "http://mealie",
                                    "mealie_token": "token", "model": "primary"}, None,
                    state, cache, False, review_store=reviews,
                    legacy_auto_import=False, approved_only=True,
                )
                repeated = process_file(
                    source, digest, {"api_key": "", "mealie_url": "http://mealie",
                                    "mealie_token": "token", "model": "primary"}, None,
                    state, cache, False, review_store=reviews,
                    legacy_auto_import=False, approved_only=True,
                )

        self.assertEqual([row["status"] for row in rows], ["IMPORTED"])
        self.assertEqual([row["status"] for row in repeated], ["ALREADY_DONE"])
        self.assertEqual([recipe.name for recipe in imported], ["Toast"])

    def test_cli_default_is_review_only_and_does_not_initialize_mealie(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "recipes"
            source.mkdir()
            scan = source / "toast.jpg"
            scan.write_bytes(b"synthetic toast")
            digest = __import__("hashlib").sha256(scan.read_bytes()).hexdigest()
            work = root / "output"
            (work / "extracted").mkdir(parents=True)
            candidate = RecipeCandidate(name="Toast", ingredients=["bread"], instructions=["Toast."])
            (work / "extracted" / f"{digest}.json").write_text(
                ExtractionBatch(recipes=[candidate]).model_dump_json(), encoding="utf-8")
            output = io.StringIO()

            with patch("import_recipes.__file__", str(root / "import_recipes.py")), \
                 patch("import_recipes.sys.argv", ["import_recipes.py", "--source", str(source)]), \
                 patch("import_recipes.load_dotenv"), \
                 patch.dict(os.environ, {"GEMINI_API_KEY": "synthetic", "GEMINI_MODEL": "test-model",
                                         "MEALIE_TOKEN": "", "MEALIE_URL": "",
                                         "RECIPE_IMPORTER_OUTPUT_DIR": ""}, clear=False), \
                 patch("pathlib.Path.cwd", return_value=root), \
                 patch("import_recipes.preflight_mealie") as preflight, \
                 patch("import_recipes.MealieClient") as mealie_client, \
                 patch("import_recipes.import_mealie") as create, redirect_stdout(output):
                code = __import__("import_recipes").main()

            with (work / "last_run_report.csv").open(encoding="utf-8-sig", newline="") as report_file:
                report = list(csv.DictReader(report_file))

        self.assertEqual(code, 0)
        self.assertIn("review-only (safe default)", output.getvalue())
        self.assertEqual([row["status"] for row in report], ["EXTRACTED_ONLY"])
        preflight.assert_not_called()
        mealie_client.assert_not_called()
        create.assert_not_called()

    def test_approved_only_mode_blocks_candidate_without_saved_approval(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "toast.jpg"
            source.write_bytes(b"synthetic toast")
            digest = __import__("hashlib").sha256(source.read_bytes()).hexdigest()
            cache = root / "extracted"
            cache.mkdir()
            candidate = RecipeCandidate(name="Toast", ingredients=["bread"], instructions=["Toast."])
            (cache / f"{digest}.json").write_text(
                ExtractionBatch(recipes=[candidate]).model_dump_json(), encoding="utf-8")
            imported = []

            with patch("import_recipes.import_mealie",
                       side_effect=lambda recipe, *_: imported.append(recipe) or "toast"):
                rows = process_file(
                    source, digest, {"api_key": "", "mealie_url": "http://mealie",
                                    "mealie_token": "token", "model": "primary"}, None,
                    StateStore(root / "state.jsonl"), cache, False,
                    legacy_auto_import=False, approved_only=True,
                )

        self.assertEqual([row["status"] for row in rows], ["NEEDS_REVIEW"])
        self.assertIn("explicitly approved", rows[0]["note"])
        self.assertEqual(imported, [])

    def test_cli_rejects_extract_only_with_import_authorization(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch("import_recipes.__file__", str(root / "import_recipes.py")), \
                 patch("import_recipes.sys.argv", ["import_recipes.py", "--extract-only",
                                                    "--import-approved"]), \
                 patch("import_recipes.load_dotenv"):
                with self.assertRaises(SystemExit) as raised:
                    __import__("import_recipes").main()

        self.assertEqual(raised.exception.code, 2)

    def test_cli_accepts_linked_identity_using_exact_effective_authority(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "toast.jpg"
            source.write_bytes(b"synthetic toast")
            digest = __import__("hashlib").sha256(source.read_bytes()).hexdigest()
            original = RecipeCandidate(name="Toast", ingredients=["bread"], instructions=["Toast."])
            current = original.model_copy(update={"suggestions": [Suggestion(
                suggestion_id="toast-detail", field_path="instructions[0]",
                proposed_value="Toast until lightly browned.", rationale="Review proposal.",
            )]})
            batch = ExtractionBatch(recipes=[current])
            extraction_id = extraction_identity(digest, batch.model_dump(mode="json"))
            cache = root / "extracted"
            cache.mkdir()
            (cache / f"{digest}.json").write_text(batch.model_dump_json(), encoding="utf-8")
            reviews = ReviewStore(root / "review.jsonl")
            prior = reviews.register_candidates(str(source), digest, "prior-extraction", [original])[0]
            target_bindings = ReviewStore(root / "target-bindings.jsonl").register_candidates(
                str(source), digest, extraction_id, [current]
            )[0]["target_bindings"]
            held_id = "held-toast-current-extraction"
            registration = reviews._base_event(
                "candidate_registration", held_id, digest, extraction_id, "held-registration"
            )
            registration.update({
                "source": str(source),
                "fingerprint": __import__("hashlib").sha256(
                    _canonical(_candidate_payload(current)).encode()
                ).hexdigest(),
                "identity_status": "needs_reconciliation",
                "candidate": current.model_dump(mode="json"), "legacy_id": "",
                "title_legacy_id": "", "legacy_aliases": [], "target_bindings": target_bindings,
            })
            with reviews.path.open("a", encoding="utf-8") as ledger:
                ledger.write(json.dumps(registration) + "\n")
            reviews = ReviewStore(reviews.path)
            reviews.confirm_identity_match(held_id, prior["candidate_id"])
            issue = next(item for item in target_bindings if item.get("issue_id") == "toast-detail")
            revision = reviews.save_revision(
                candidate_id=held_id, source_hash=digest, extraction_id=extraction_id,
                expected_parent=0, operation_id="save-linked-toast-review",
                recipe=current.model_dump(mode="json"), target_bindings=[
                    {"target_id": issue["source_target_id"], "field": "instructions", "position": 0}
                ],
            )
            reviews.resolve_issue(
                candidate_id=held_id, source_hash=digest, extraction_id=extraction_id,
                revision=revision["revision"], issue_id=issue["issue_id"],
                target_id=issue["target_id"], disposition="accepted",
                final_value="Toast until lightly browned.", operation_id="accept-linked-toast-proposal",
            )
            reviews.approve_revision(
                candidate_id=held_id, source_hash=digest, extraction_id=extraction_id,
                revision=revision["revision"], operation_id="approve-linked-toast-review",
                source_path=str(source),
            )
            imported = []

            with patch("import_recipes.import_mealie",
                       side_effect=lambda recipe, *_: imported.append(recipe) or "toast"):
                rows = process_file(
                    source, digest, {"api_key": "", "mealie_url": "http://mealie",
                                    "mealie_token": "token", "model": "primary"}, None,
                    StateStore(root / "state.jsonl"), cache, False, review_store=reviews,
                    legacy_auto_import=False, approved_only=True,
                )

        self.assertEqual(reviews.registrations[held_id][0]["identity_status"], "needs_reconciliation")
        self.assertEqual(reviews.identity_link_status(held_id, digest, extraction_id), "registered")
        self.assertEqual([row["status"] for row in rows], ["IMPORTED"])
        self.assertEqual([recipe.name for recipe in imported], ["Toast"])

    def test_forced_renamed_retry_is_held_when_same_source_has_an_imported_sibling(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "multi-recipe.jpg"
            source.write_bytes(b"synthetic multi-recipe source")
            digest = __import__("hashlib").sha256(source.read_bytes()).hexdigest()
            cache = root / "extracted"
            cache.mkdir()
            state = StateStore(root / "import_state.jsonl")
            reviews = ReviewStore(root / "review_events.jsonl", state_path=state.path)
            imported_sibling = RecipeCandidate(
                name="Imported Dish", ingredients=["1 cup rice"], instructions=["Cook rice."],
            )
            unresolved_sibling = RecipeCandidate(
                name="Second Dish", ingredients=["2 cups beans"], instructions=[],
            )
            renamed_retry = RecipeCandidate(
                name="Renamed Second Dish", ingredients=["2 cups beans"], instructions=["Simmer."],
            )
            config = {"api_key": "", "mealie_url": "http://mealie",
                      "mealie_token": "test-token", "model": "primary"}
            posts = []

            with patch("import_recipes.extract", side_effect=[
                ExtractionBatch(recipes=[imported_sibling, unresolved_sibling]),
                ExtractionBatch(recipes=[renamed_retry, imported_sibling]),
            ]), patch("import_recipes.import_mealie",
                      side_effect=lambda recipe, *_: posts.append(recipe.name) or "created-slug"):
                first = process_file(source, digest, config, None, state, cache, False,
                                     review_store=reviews)
                second = process_file(source, digest, config, None, state, cache, False,
                                      force_extract=True, review_store=reviews)

        self.assertEqual(posts, ["Imported Dish"])
        self.assertEqual([row["status"] for row in first], ["IMPORTED", "INCOMPLETE"])
        self.assertEqual([row["status"] for row in second], ["NEEDS_REVIEW", "ALREADY_DONE"])
        self.assertEqual(second[0]["title"], "Renamed Second Dish")

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
