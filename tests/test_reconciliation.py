import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from import_recipes import (
    AmbiguousCandidate,
    StateStore,
    candidate_identity,
    process_file,
)
from recipe_quality import ExtractionBatch, RecipeCandidate


class ReconciliationTests(unittest.TestCase):
    def test_legacy_hash_index_state_is_still_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.jsonl"
            path.write_text(json.dumps({
                "key": "abc:0", "status": "IMPORTED", "slug": "toast",
                "file": "scan.jpg", "title": "Toast",
            }) + "\n", encoding="utf-8")
            state = StateStore(path)

            match = state.find_candidate("abc", 0, RecipeCandidate(
                name="Toast", ingredients=["bread"], instructions=["Toast."]
            ))

        self.assertEqual(match["slug"], "toast")

    def test_reordered_candidates_match_unique_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = StateStore(Path(tmp) / "state.jsonl")
            a = RecipeCandidate(name="Apple Pie", ingredients=["apples"], instructions=["Bake."])
            b = RecipeCandidate(name="Bean Soup", ingredients=["beans"], instructions=["Boil."])
            state.add("hash:0", "IMPORTED", "apple", "scan.jpg", a.name,
                      identity=candidate_identity("hash", a))
            state.add("hash:1", "INCOMPLETE", "", "scan.jpg", b.name,
                      identity=candidate_identity("hash", b))

            match = state.find_candidate("hash", 0, b)

        self.assertEqual(match["title"], "Bean Soup")

    def test_legacy_records_derive_identity_for_reordered_candidates(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.jsonl"
            path.write_text("\n".join([
                json.dumps({"key": "hash:0", "status": "IMPORTED", "slug": "apple", "file": "scan.jpg", "title": "Apple Pie"}),
                json.dumps({"key": "hash:1", "status": "IMPORTED", "slug": "bean", "file": "scan.jpg", "title": "Bean Soup"}),
            ]) + "\n", encoding="utf-8")
            state = StateStore(path)
            apple = RecipeCandidate(name="Apple Pie", ingredients=["apples"], instructions=["Bake."])
            bean = RecipeCandidate(name="Bean Soup", ingredients=["beans"], instructions=["Boil."])

            first = state.find_candidate("hash", 0, bean)
            second = state.find_candidate("hash", 1, apple)

        self.assertEqual(first["slug"], "bean")
        self.assertEqual(second["slug"], "apple")

    def test_duplicate_identity_requires_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = StateStore(Path(tmp) / "state.jsonl")
            first = RecipeCandidate(name="Cake", ingredients=["flour"], instructions=["Bake."])
            second = RecipeCandidate(name="Cake", ingredients=["cocoa"], instructions=["Bake."])
            identity = candidate_identity("hash", first)
            state.add("hash:0", "IMPORTED", "cake-one", "scan.jpg", first.name, identity=identity)
            state.add("hash:1", "IMPORTED", "cake-two", "scan.jpg", second.name, identity=identity)

            with self.assertRaises(AmbiguousCandidate):
                state.find_candidate("hash", 0, RecipeCandidate(
                    name="Cake", ingredients=["sugar"], instructions=["Bake."]
                ))

    def test_retry_reordered_multi_recipe_file_imports_only_unresolved_recipe(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "scan.jpg"
            source.write_bytes(b"scan")
            cache = root / "cache"
            cache.mkdir()
            digest = "hash"
            old = ExtractionBatch(recipes=[
                RecipeCandidate(name="Apple Pie", ingredients=["apples"], instructions=["Bake."]),
                RecipeCandidate(name="Bean Soup", ingredients=[], instructions=["Boil."]),
            ])
            (cache / f"{digest}.json").write_text(old.model_dump_json(), encoding="utf-8")
            state = StateStore(root / "state.jsonl")
            apple = old.recipes[0]
            state.add("hash:0", "IMPORTED", "apple", str(source), apple.name,
                      identity=candidate_identity(digest, apple))
            new = ExtractionBatch(recipes=[
                RecipeCandidate(name="Bean Soup", ingredients=["beans"], instructions=["Boil."]),
                RecipeCandidate(name="Apple Pie", ingredients=["apples"], instructions=["Bake."]),
            ])
            imported = []

            with patch("import_recipes.extract", return_value=new), \
                 patch("import_recipes.file_digest", return_value=digest), \
                 patch("import_recipes.import_mealie", side_effect=lambda recipe, *_: imported.append(recipe.name) or "bean"):
                rows = process_file(
                    source, digest,
                    {"api_key": "", "mealie_url": "http://mealie", "mealie_token": "token", "model": "primary"},
                    None, state, cache, False, retry_incomplete=True, force_extract=True,
                )

        self.assertEqual(imported, ["Bean Soup"])
        self.assertEqual({row["status"] for row in rows}, {"IMPORTED", "ALREADY_DONE"})


if __name__ == "__main__":
    unittest.main()
