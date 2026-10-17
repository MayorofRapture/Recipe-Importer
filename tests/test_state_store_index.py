import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from import_recipes import AmbiguousCandidate, StateStore, candidate_identity
from recipe_quality import RecipeCandidate
from ui_data import _state_record


class NoIterationDict(dict):
    def values(self):
        raise AssertionError("candidate lookup must use the snapshot index")

    def items(self):
        raise AssertionError("candidate lookup must use the snapshot index")

    def __iter__(self):
        raise AssertionError("candidate lookup must use the snapshot index")


class StateStoreIndexTests(unittest.TestCase):
    def test_lookup_uses_prebuilt_indexes_with_large_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.jsonl"
            records = []
            for i in range(1000):
                candidate = RecipeCandidate(name=f"Recipe {i}", ingredients=["x"], instructions=["y"])
                records.append(json.dumps({"key": f"hash-{i}:0", "status": "IMPORTED",
                                           "title": candidate.name,
                                           "identity": candidate_identity(f"hash-{i}", candidate)}))
            path.write_text("\n".join(records) + "\n", encoding="utf-8")
            state = StateStore(path)
            wanted = RecipeCandidate(name="Recipe 517", ingredients=["x"], instructions=["y"])
            state.records = NoIterationDict(state.records)

            match = state.find_candidate("hash-517", 0, wanted)

        self.assertEqual(match["title"], "Recipe 517")

    def test_indexes_refresh_after_add_and_refresh(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.jsonl"
            state = StateStore(path)
            candidate = RecipeCandidate(name="Toast", ingredients=["bread"], instructions=["toast"])
            identity = candidate_identity("hash", candidate)
            state.add("hash:0", "IMPORTED", "toast", "toast.jpg", "Toast", identity=identity)
            self.assertEqual(state.find_candidate("hash", 3, candidate)["slug"], "toast")

            # A second store models an external CLI writer; refresh must replace its snapshot and indexes.
            other = StateStore(path)
            other.add("next-hash:0", "INCOMPLETE", "", "next.jpg", "Next")
            state.refresh()
            next_candidate = RecipeCandidate(name="Next", ingredients=[], instructions=["bake"])
            self.assertEqual(state.find_candidate("next-hash", 0, next_candidate)["title"], "Next")

    def test_replacing_a_key_removes_its_old_index_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = StateStore(Path(tmp) / "state.jsonl")
            old = RecipeCandidate(name="Old", ingredients=[], instructions=["bake"])
            state.add("hash:0", "INCOMPLETE", "", "scan.jpg", old.name,
                      identity=candidate_identity("hash", old), locked=True)
            new = RecipeCandidate(name="New", ingredients=[], instructions=["boil"])
            state.add("hash:0", "INCOMPLETE", "", "scan.jpg", new.name,
                      identity=candidate_identity("hash", new), locked=True)

            self.assertIsNone(state.find_candidate("hash", 8, old))
            self.assertEqual(state.find_candidate("hash", 8, new)["title"], "New")

    def test_duplicate_legacy_matches_remain_ambiguous(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = StateStore(Path(tmp) / "state.jsonl")
            state.add("hash:0", "IMPORTED", "one", "a.jpg", "Cake")
            state.add("hash:1", "IMPORTED", "two", "b.jpg", "Cake")

            with self.assertRaises(AmbiguousCandidate):
                state.find_candidate("hash", 0, RecipeCandidate(name="Cake", ingredients=[], instructions=["bake"]))

    def test_identity_alias_and_source_title_indexes_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = StateStore(Path(tmp) / "state.jsonl")
            candidate = RecipeCandidate(name="Apple Pie", ingredients=["apples"], instructions=["Bake"])
            alias = "hash:historical-id"
            state.add("hash:0", "IMPORTED", "pie", "scan.jpg", candidate.name, identity=alias)
            self.assertEqual(state.find_candidate("hash", 9, candidate, (alias,))["slug"], "pie")

    def test_catalog_state_projection_calls_candidate_lookup_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = StateStore(Path(tmp) / "state.jsonl")
            candidate = RecipeCandidate(name="Toast", ingredients=["bread"], instructions=["toast"])
            state.add("hash:0", "IMPORTED", "toast", "toast.jpg", candidate.name,
                      identity=candidate_identity("hash", candidate), locked=True)
            with patch.object(state, "find_candidate", wraps=state.find_candidate) as lookup:
                record = _state_record(state, candidate_identity("hash", candidate), "hash", candidate)

        self.assertEqual(record["slug"], "toast")
        lookup.assert_called_once()


if __name__ == "__main__":
    unittest.main()
