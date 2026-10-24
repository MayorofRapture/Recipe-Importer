import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from recipe_quality import RecipeCandidate
from review_store import ReviewStore, _canonical


class NoRegistrationIteration(dict):
    def values(self):
        raise AssertionError("exact registration lookup must use its snapshot index")


class ReviewRegistrationIndexTests(unittest.TestCase):
    def test_exact_registration_lookup_uses_index_for_populated_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "review_events.jsonl"
            events = []
            target = None
            for index in range(500):
                candidate = RecipeCandidate(name=f"Recipe {index}", ingredients=["x"], instructions=["y"])
                fingerprint = hashlib.sha256(
                    _canonical(candidate.model_dump(mode="json")).encode(),
                ).hexdigest()
                event = {
                    "event": "candidate_registration", "candidate_id": f"id-{index}",
                    "source_hash": f"source-{index}", "extraction_id": f"extract-{index}",
                    "fingerprint": fingerprint, "identity_status": "registered",
                }
                events.append(json.dumps(event))
                if index == 317:
                    target = (candidate, event)
            path.write_text("\n".join(events) + "\n", encoding="utf-8")
            reviews = ReviewStore(path)
            candidate, expected = target
            reviews.registrations = NoRegistrationIteration(reviews.registrations)

            match = reviews._exact_registration_batch("source-317", "extract-317", [candidate])

        self.assertEqual(match, [expected])

    def test_registration_mutation_updates_snapshot_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            reviews = ReviewStore(Path(tmp) / "review_events.jsonl")
            candidate = RecipeCandidate(name="Toast", ingredients=["bread"], instructions=["toast"])
            registered = reviews.register_candidates("scan.jpg", "hash", "extraction", [candidate])

            match = reviews._exact_registration_batch("hash", "extraction", [candidate])

        self.assertEqual(match, registered)


if __name__ == "__main__":
    unittest.main()
