import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from draft_store import DraftStore
from import_recipes import StateStore, candidate_identity, file_digest, process_file
from recipe_quality import ExtractionBatch, RecipeCandidate
from review_store import ReviewStore


class LegacyReviewProjectionTests(unittest.TestCase):
    def test_newer_legacy_draft_blocks_cli_import_of_older_approval(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "recipe.jpg"
            source.write_bytes(b"synthetic scan")
            digest = file_digest(source)
            cache = root / "extracted"
            cache.mkdir()
            recipe = RecipeCandidate(name="Rice", ingredients=["1 cup rice"],
                                     instructions=["Cook."], requires_review=True)
            (cache / f"{digest}.json").write_text(
                ExtractionBatch(recipes=[recipe]).model_dump_json(), encoding="utf-8")
            reviews = ReviewStore(root / "review_events.jsonl")
            identity = candidate_identity(digest, recipe)
            reviews.add_candidate(str(source), identity, recipe)
            reviews.record_candidate_decision(str(source), identity, "approved")
            edited = recipe.model_copy(deep=True)
            edited.ingredients = ["2 cups rice"]
            DraftStore(root / "ui_drafts.jsonl").save(
                identity, digest, str(source), edited.model_dump(mode="json"),
                "2 cups rice", "Cook.", "structured", [])
            with patch("import_recipes.import_mealie", return_value="rice") as create:
                rows = process_file(source, digest,
                                    {"model": "fixture", "api_key": "", "mealie_url": "",
                                     "mealie_token": ""}, None,
                                    StateStore(root / "import_state.jsonl"), cache, False,
                                    review_store=reviews)
            create.assert_not_called()
            self.assertEqual(rows[0]["status"], "NEEDS_REVIEW")
