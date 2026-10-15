import tempfile
import unittest
from pathlib import Path

from draft_store import DraftStore


class DraftStoreTests(unittest.TestCase):
    def test_drafts_survive_restart_and_increment_revisions(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "drafts.jsonl"
            first = DraftStore(path).save(
                candidate_id="hash:recipe",
                source_hash="hash",
                source_path="scan.jpg",
                recipe={"name": "Fried Rice", "ingredients": ["rice"], "instructions": ["Cook."]},
                raw_ingredients="rice",
                raw_instructions="Cook.",
                mode="quick",
                warnings=["check title"],
            )
            second = DraftStore(path).save(
                candidate_id="hash:recipe",
                source_hash="hash",
                source_path="scan.jpg",
                recipe={"name": "Fried Rice", "ingredients": ["2 cups rice"], "instructions": ["Cook."]},
                raw_ingredients="2 cups rice",
                raw_instructions="Cook.",
                mode="structured",
                warnings=[],
            )
            restored = DraftStore(path).latest("hash:recipe")

        self.assertEqual(first.revision, 1)
        self.assertEqual(second.revision, 2)
        self.assertEqual(restored.revision, 2)
        self.assertEqual(restored.recipe["ingredients"], ["2 cups rice"])
        self.assertEqual(restored.mode, "structured")

    def test_different_candidates_have_independent_latest_drafts(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = DraftStore(Path(tmp) / "drafts.jsonl")
            store.save("hash:a", "hash", "a.jpg", {"name": "A"}, "a", "step", "quick", [])
            store.save("hash:b", "hash", "b.jpg", {"name": "B"}, "b", "step", "quick", [])

            self.assertEqual(store.latest("hash:a").recipe["name"], "A")
            self.assertEqual(store.latest("hash:b").recipe["name"], "B")


if __name__ == "__main__":
    unittest.main()
