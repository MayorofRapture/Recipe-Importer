import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

from mealie_client import MealieClient
from import_recipes import StateStore, process_file
from recipe_quality import ExtractionBatch, RecipeCandidate


class Response:
    def __init__(self, status, payload=None, text=""):
        self.status_code = status
        self._payload = payload
        self.text = text

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeSession:
    def __init__(self):
        self.calls = []
        self.tags = [{"id": "old-id", "name": "Dinner", "slug": "dinner"}]
        self.recipe = {"slug": "toast", "name": "Toast", "tags": [self.tags[0]]}

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        if "/organizers/tags" in url:
            return Response(200, {"items": self.tags})
        return Response(200, self.recipe)

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        tag = {"id": "batch-id", "name": kwargs["json"]["name"], "slug": "batch"}
        self.tags.append(tag)
        return Response(201, tag)

    def put(self, url, **kwargs):
        self.calls.append(("PUT", url, kwargs))
        self.recipe = kwargs["json"]
        return Response(200, self.recipe)


class MealieTagTests(unittest.TestCase):
    def test_missing_tag_is_created_and_new_tag_preserves_existing_recipe_tags(self):
        session = FakeSession()
        client = MealieClient("http://mealie", "token", session=session)

        tag = client.ensure_tag("Batch 2026")
        client.add_tag("toast", tag)

        self.assertEqual(tag["name"], "Batch 2026")
        self.assertEqual([item["name"] for item in session.recipe["tags"]], ["Dinner", "Batch 2026"])
        self.assertEqual([call[0] for call in session.calls], ["GET", "POST", "GET", "PUT"])

    def test_existing_tag_is_idempotent_without_recipe_update(self):
        session = FakeSession()
        client = MealieClient("http://mealie", "token", session=session)

        client.add_tag("toast", client.ensure_tag("Dinner"))

        self.assertEqual([call[0] for call in session.calls], ["GET", "GET"])
        self.assertEqual(len(session.recipe["tags"]), 1)

    def test_scoped_backfill_only_updates_requested_slugs(self):
        session = FakeSession()
        client = MealieClient("http://mealie", "token", session=session)

        result = client.backfill_tag(["toast"], "Dinner")

        self.assertEqual(result, {"toast": "already_present"})
        self.assertFalse(any(call[0] == "POST" for call in session.calls))

    def test_tag_failure_is_repaired_without_duplicate_recipe_creation(self):
        class Tagger:
            def __init__(self):
                self.failed = True

            def ensure_tag(self, name):
                return {"id": "batch", "name": name, "slug": "batch"}

            def add_tag(self, slug, tag):
                if self.failed:
                    self.failed = False
                    raise RuntimeError("tag service unavailable")
                return "tagged"

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "scan.jpg"
            source.write_bytes(b"scan")
            cache = root / "cache"
            cache.mkdir()
            candidate = RecipeCandidate(name="Toast", ingredients=["bread"], instructions=["Toast."])
            (cache / "hash.json").write_text(ExtractionBatch(recipes=[candidate]).model_dump_json(), encoding="utf-8")
            state = StateStore(root / "state.jsonl")
            tagger = Tagger()
            creates = []
            with patch("import_recipes.import_mealie", side_effect=lambda *args: creates.append("create") or "toast"):
                first = process_file(source, "hash", {"api_key": "", "model": "primary", "mealie_url": "", "mealie_token": ""}, None, state, cache, False, batch_tag="Batch", mealie_client=tagger)
                second = process_file(source, "hash", {"api_key": "", "model": "primary", "mealie_url": "", "mealie_token": ""}, None, state, cache, False, batch_tag="Batch", mealie_client=tagger)

        self.assertEqual(creates, ["create"])
        self.assertEqual(first[0]["status"], "TAG_FAILED")
        self.assertEqual(second[0]["status"], "TAGGED_REPAIRED")


if __name__ == "__main__":
    unittest.main()
