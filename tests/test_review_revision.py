"""R1.1 regression cases for draft, approval, and sibling identity boundaries."""
from __future__ import annotations

import json
import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from draft_store import DraftStore
from import_recipes import StateStore, candidate_identity, process_file
from recipe_quality import RecipeCandidate, schema_recipe
from review_store import ReviewStore
from tests.ui_fixture_factory import write_ui_fixture
from ui_data import load_catalog


APP_PATH = Path(__file__).resolve().parents[1] / "streamlit_app.py"


class ReviewRevisionTests(unittest.TestCase):
    def test_same_source_same_title_candidates_have_distinct_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = write_ui_fixture(Path(tmp), duplicate_titles=True)
            candidates = [
                view.candidate for view in load_catalog(Path(fixture["output"]))
                if view.candidate.name == "Fried Rice"
            ]

        self.assertNotEqual(
            candidate_identity(str(fixture["digest"]), candidates[0]),
            candidate_identity(str(fixture["digest"]), candidates[1]),
        )

    def test_duplicate_titles_do_not_share_review_approval(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = write_ui_fixture(Path(tmp), duplicate_titles=True)
            output = Path(fixture["output"])
            candidates = [
                view.candidate for view in load_catalog(output)
                if view.candidate.name == "Fried Rice"
            ]
            self.assertEqual(len(candidates), 2)
            reviews = ReviewStore(output / "review_events.jsonl")
            reviews.record_candidate_decision(
                str(fixture["source"]), candidate_identity(str(fixture["digest"]), candidates[0]),
                "approved",
            )
            state = StateStore(output / "import_state.jsonl")

            with patch("import_recipes.import_mealie") as create_recipe:
                rows = process_file(
                    Path(fixture["source"]), str(fixture["digest"]),
                    {"api_key": "unused", "mealie_url": "http://mealie.test",
                     "mealie_token": "unused", "model": "fixture"},
                    None, state, output / "extracted", True,
                    review_store=reviews,
                )

        fried_rows = [row for row in rows if row["title"] == "Fried Rice"]
        self.assertEqual([row["status"] for row in fried_rows], ["EXTRACTED_ONLY", "NEEDS_REVIEW"])
        self.assertNotEqual(fried_rows[0]["candidate_id"], fried_rows[1]["candidate_id"])
        create_recipe.assert_not_called()

    def test_duplicate_titles_do_not_share_import_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = write_ui_fixture(
                Path(tmp), duplicate_titles=True, duplicate_requires_review=False,
            )
            output = Path(fixture["output"])
            candidates = [
                view.candidate for view in load_catalog(output)
                if view.candidate.name == "Fried Rice"
            ]
            state = StateStore(output / "import_state.jsonl")
            state.add(
                f"{fixture['digest']}:0", "IMPORTED", "first-fried-rice",
                str(fixture["source"]), candidates[0].name,
                identity=candidate_identity(str(fixture["digest"]), candidates[0]),
            )
            created = []

            with patch("import_recipes.import_mealie",
                       side_effect=lambda recipe, *_: created.append(recipe) or "second-fried-rice"):
                rows = process_file(
                    Path(fixture["source"]), str(fixture["digest"]),
                    {"api_key": "unused", "mealie_url": "http://mealie.test",
                     "mealie_token": "unused", "model": "fixture"},
                    None, state, output / "extracted", False,
                    review_store=ReviewStore(output / "review_events.jsonl"),
                )
            events = [json.loads(line)
                      for line in (output / "review_events.jsonl").read_text(encoding="utf-8").splitlines()]

        fried_rows = [row for row in rows if row["title"] == "Fried Rice"]
        self.assertEqual([row["status"] for row in fried_rows], ["ALREADY_DONE", "IMPORTED"])
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0].ingredients, ["1 cup cold rice"])
        self.assertNotEqual(fried_rows[0]["candidate_id"], fried_rows[1]["candidate_id"])

    def test_legacy_duplicate_title_imports_remain_protected_until_reconciliation(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = write_ui_fixture(Path(tmp), duplicate_titles=True)
            output = Path(fixture["output"])
            legacy_identity = f"{fixture['digest']}:{hashlib.sha256('fried rice'.encode()).hexdigest()[:20]}"
            state = StateStore(output / "import_state.jsonl")
            state.add(f"{fixture['digest']}:0", "IMPORTED", "legacy-one", str(fixture["source"]),
                      "Fried Rice", identity=legacy_identity)
            state.add(f"{fixture['digest']}:4", "IMPORTED", "legacy-two", str(fixture["source"]),
                      "Fried Rice", identity=legacy_identity)
            with patch("import_recipes.import_mealie") as create_recipe:
                rows = process_file(
                    Path(fixture["source"]), str(fixture["digest"]),
                    {"api_key": "unused", "mealie_url": "http://mealie.test",
                     "mealie_token": "unused", "model": "fixture"},
                    None, state, output / "extracted", False,
                    review_store=ReviewStore(output / "review_events.jsonl"),
                )

        fried_rows = [row for row in rows if row["title"] == "Fried Rice"]
        self.assertEqual([row["status"] for row in fried_rows], ["NEEDS_REVIEW", "NEEDS_REVIEW"])
        create_recipe.assert_not_called()

    def test_accept_suggestion_after_saved_draft_survives_reload(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = write_ui_fixture(Path(tmp))
            output = Path(fixture["output"])
            views = load_catalog(output, Path(tmp) / "sources")
            view = next(item for item in views if item.candidate.name == "Cranberry & Caramel Date Bars")
            DraftStore(output / "ui_drafts.jsonl").save(
                view.candidate_id, view.source_hash, view.source_path,
                view.candidate.model_dump(mode="json"),
                "dates\ncranberries", "Mix and bake.", "structured", [],
            )
            ReviewStore(output / "review_events.jsonl").record(
                view.source_path, view.candidate_id, view.candidate,
                "cranberry-quantity", "approved",
            )

            reloaded = next(
                item for item in load_catalog(output, Path(tmp) / "sources")
                if item.candidate.name == "Cranberry & Caramel Date Bars"
            )

        self.assertEqual(reloaded.candidate.ingredients[0], "1 cup dates")
        self.assertEqual(reloaded.candidate.suggestions[0].review_status, "approved")

    def test_content_changed_after_approval_is_not_import_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = write_ui_fixture(Path(tmp))
            output = Path(fixture["output"])
            view = next(item for item in load_catalog(output) if item.candidate.name == "Fried Rice")
            reviews = ReviewStore(output / "review_events.jsonl")
            reviews.record_candidate_decision(view.source_path, view.candidate_id, "approved")
            changed = view.candidate.model_copy(deep=True)
            changed.ingredients[0] = "3 cups rice"
            DraftStore(output / "ui_drafts.jsonl").save(
                view.candidate_id, view.source_hash, view.source_path,
                changed.model_dump(mode="json"), "3 cups rice\n1 egg",
                "Cook rice.\nFold in egg.", "structured", [],
            )

            reloaded = next(item for item in load_catalog(output) if item.candidate.name == "Fried Rice")

        self.assertNotEqual(reloaded.status, "Ready to import")
        self.assertFalse(reloaded.import_ready)

    def test_new_approval_requires_hash_matching_source_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = write_ui_fixture(Path(tmp))
            source = Path(fixture["source"])
            output = Path(fixture["output"])
            before = next(item for item in load_catalog(output) if item.candidate.name == "Fried Rice")
            self.assertTrue(before.source_available)
            source_bytes = source.read_bytes()
            source.unlink()
            after = next(item for item in load_catalog(output) if item.candidate.name == "Fried Rice")
            relink_root = Path(tmp) / "relinked"
            relink_root.mkdir()
            (relink_root / source.name).write_bytes(source_bytes)
            relinked = next(item for item in load_catalog(output, relink_root)
                            if item.candidate.name == "Fried Rice")

        self.assertFalse(after.source_available)
        self.assertTrue(relinked.source_available)
        self.assertEqual(relinked.source_path, str(relink_root / "cookbook.pdf"))

    def test_empty_recipe_is_not_approved(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = write_ui_fixture(Path(tmp))
            output = Path(fixture["output"])
            with patch.dict(os.environ, {
                "RECIPE_IMPORTER_OUTPUT_DIR": str(output),
                "RECIPE_IMPORTER_SOURCE_DIR": str(Path(tmp) / "sources"),
            }, clear=False):
                app = AppTest.from_file(str(APP_PATH)).run(timeout=10)
                for label in ("Ingredient 1", "Ingredient 2"):
                    app = next(item for item in app.text_input if item.label == label).set_value("").run(timeout=10)
                candidate_id = app.session_state["review_selection"]
                app.session_state[f"recipe_sections:{candidate_id}"] = "Instructions"
                app.run(timeout=10)
                for label in ("Step 1", "Step 2"):
                    app = next(item for item in app.text_area if item.label == label).set_value("").run(timeout=10)
                approve = next(button for button in app.button if "Approve & next" in button.label)
                self.assertTrue(approve.disabled)

            view = next(item for item in load_catalog(output) if item.candidate.name == "Fried Rice")
            decision = ReviewStore(output / "review_events.jsonl").candidate_decision(view.candidate_id)

        self.assertIsNone(decision)

    def test_structured_reorder_survives_widget_rerun(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = write_ui_fixture(Path(tmp))
            with patch.dict(os.environ, {
                "RECIPE_IMPORTER_OUTPUT_DIR": str(fixture["output"]),
                "RECIPE_IMPORTER_SOURCE_DIR": str(Path(tmp) / "sources"),
            }, clear=False):
                app = AppTest.from_file(str(APP_PATH)).run(timeout=10)
                next(item for item in app.menu_button if item.label == "Actions").click("Move down").run(timeout=10)

        ingredients = [item.value for item in app.text_input if item.label.startswith("Ingredient ")]
        self.assertEqual(ingredients, ["1 egg", "2 cups rice"])

    def test_structured_remove_then_edit_targets_remaining_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = write_ui_fixture(Path(tmp))
            with patch.dict(os.environ, {
                "RECIPE_IMPORTER_OUTPUT_DIR": str(fixture["output"]),
                "RECIPE_IMPORTER_SOURCE_DIR": str(Path(tmp) / "sources"),
            }, clear=False):
                app = AppTest.from_file(str(APP_PATH)).run(timeout=10)
                next(item for item in app.menu_button if item.label == "Actions").click("Remove").run(timeout=10)
                next(item for item in app.text_input if item.label == "Ingredient 1").set_value("2 eggs").run(timeout=10)

        values = [item.value for item in app.text_input if item.label.startswith("Ingredient ")]
        self.assertEqual(values, ["2 eggs"])

    def test_approve_action_can_advance_after_recipe_selector_renders(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = write_ui_fixture(Path(tmp))
            with patch.dict(os.environ, {
                "RECIPE_IMPORTER_OUTPUT_DIR": str(fixture["output"]),
                "RECIPE_IMPORTER_SOURCE_DIR": str(Path(tmp) / "sources"),
            }, clear=False):
                app = AppTest.from_file(str(APP_PATH)).run(timeout=10)
                next(button for button in app.button if "Approve & next" in button.label).click().run(timeout=10)

        self.assertFalse(app.exception)

    def test_all_proposals_for_a_field_and_unindexed_proposal_are_rendered(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = write_ui_fixture(Path(tmp))
            with patch.dict(os.environ, {
                "RECIPE_IMPORTER_OUTPUT_DIR": str(fixture["output"]),
                "RECIPE_IMPORTER_SOURCE_DIR": str(Path(tmp) / "sources"),
            }, clear=False):
                app = AppTest.from_file(str(APP_PATH)).run(timeout=10)
                selector = next(item for item in app.selectbox if item.label == "Jump to recipe")
                app = selector.set_value("Cranberry & Caramel Date Bars").run(timeout=10)
                suggestion_actions = [button.label for button in app.button if button.label == "Use suggestion"]

        self.assertEqual(len(suggestion_actions), 3)

    def test_ui_approved_draft_is_the_cli_import_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = write_ui_fixture(Path(tmp))
            output = Path(fixture["output"])
            with patch.dict(os.environ, {
                "RECIPE_IMPORTER_OUTPUT_DIR": str(output),
                "RECIPE_IMPORTER_SOURCE_DIR": str(Path(tmp) / "sources"),
            }, clear=False):
                app = AppTest.from_file(str(APP_PATH)).run(timeout=10)
                next(item for item in app.text_input if item.label == "Ingredient 1").set_value("3 cups rice").run(timeout=10)
                next(button for button in app.button if "Approve & next" in button.label).click().run(timeout=10)

            created = []
            with patch("import_recipes.import_mealie",
                       side_effect=lambda recipe, *_: created.append(recipe) or "fried-rice"):
                rows = process_file(
                    Path(fixture["source"]), str(fixture["digest"]),
                    {"api_key": "unused", "mealie_url": "http://mealie.test",
                     "mealie_token": "unused", "model": "fixture"},
                    None, StateStore(output / "import_state.jsonl"), output / "extracted", False,
                    review_store=ReviewStore(output / "review_events.jsonl"),
                )
            events = [json.loads(line)
                      for line in (output / "review_events.jsonl").read_text(encoding="utf-8").splitlines()]

        fried_rows = [row for row in rows if row["title"] == "Fried Rice"]
        self.assertEqual([row["status"] for row in fried_rows], ["IMPORTED"])
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0].ingredients, ["3 cups rice", "1 egg"])
        revision = next(item for item in events if item.get("event") == "review_revision"
                        and item.get("candidate_id") == fried_rows[0]["candidate_id"])
        approval = next(item for item in events if item.get("event") == "review_approval"
                        and item.get("candidate_id") == fried_rows[0]["candidate_id"])
        self.assertEqual(approval["content_sha256"], revision["content_sha256"])
        self.assertEqual(schema_recipe(created[0]), schema_recipe(RecipeCandidate.model_validate(revision["recipe"])))

    def test_ui_proposal_resolution_writes_immutable_issue_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = write_ui_fixture(Path(tmp))
            output = Path(fixture["output"])
            with patch.dict(os.environ, {
                "RECIPE_IMPORTER_OUTPUT_DIR": str(output),
                "RECIPE_IMPORTER_SOURCE_DIR": str(Path(tmp) / "sources"),
            }, clear=False):
                app = AppTest.from_file(str(APP_PATH)).run(timeout=10)
                app = next(item for item in app.selectbox if item.label == "Jump to recipe").set_value(
                    "Cranberry & Caramel Date Bars").run(timeout=10)
                next(button for button in app.button if button.label == "Use suggestion").click().run(timeout=10)
            events = [json.loads(line) for line in (output / "review_events.jsonl").read_text(encoding="utf-8").splitlines()]

        self.assertFalse(app.exception)
        resolution = next(item for item in events if item.get("event") == "review_issue_resolution")
        self.assertTrue(resolution["issue_id"])
        self.assertTrue(resolution["target_id"])
        self.assertEqual(resolution["disposition"], "accepted")

    def test_proposal_resolution_preserves_other_unsaved_editor_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = write_ui_fixture(Path(tmp))
            output = Path(fixture["output"])
            with patch.dict(os.environ, {
                "RECIPE_IMPORTER_OUTPUT_DIR": str(output),
                "RECIPE_IMPORTER_SOURCE_DIR": str(Path(tmp) / "sources"),
            }, clear=False):
                app = AppTest.from_file(str(APP_PATH)).run(timeout=10)
                app = next(item for item in app.selectbox if item.label == "Jump to recipe").set_value(
                    "Cranberry & Caramel Date Bars").run(timeout=10)
                next(item for item in app.text_input if item.label == "Ingredient 2").set_value(
                    "fresh cranberries").run(timeout=10)
                next(button for button in app.button if button.label == "Use suggestion").click().run(timeout=10)
            events = [json.loads(line) for line in (output / "review_events.jsonl").read_text(encoding="utf-8").splitlines()]

        revision = next(item for item in events if item.get("event") == "review_revision"
                        and item.get("recipe", {}).get("name") == "Cranberry & Caramel Date Bars")
        self.assertFalse(app.exception)
        self.assertEqual(revision["recipe"]["ingredients"][1], "fresh cranberries")


if __name__ == "__main__":
    unittest.main()
