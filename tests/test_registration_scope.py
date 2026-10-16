import tempfile
import unittest
from pathlib import Path

from recipe_quality import RecipeCandidate
from review_store import ReviewStore


class RegistrationScopeTests(unittest.TestCase):
    def test_identical_recipe_in_different_source_does_not_inherit_legacy_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ReviewStore(Path(tmp) / "review_events.jsonl")
            recipe = RecipeCandidate(name="Synthetic rice", ingredients=["1 cup rice"], instructions=["Cook."])
            store.add_candidate("one.jpg", "source-one:legacy-title", recipe)
            registration = store.register_candidates("two.jpg", "source-two", "extraction-two", [recipe])[0]
            self.assertNotEqual(registration["candidate_id"], "source-one:legacy-title")
