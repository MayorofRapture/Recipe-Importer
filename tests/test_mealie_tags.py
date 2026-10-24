import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

from mealie_client import MealieClient
from import_recipes import StateStore, file_digest, process_file
from recipe_quality import ExtractionBatch, RecipeCandidate
from review_store import ReviewStore, extraction_identity


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
                    # A definite rejection is safe to retry automatically on
                    # the next run; an outcome-unknown error needs explicit
                    # read-before-write reconciliation instead.
                    raise RuntimeError("HTTP 400")
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

    def test_extract_only_never_repairs_failed_tag_or_changes_import_ledgers(self):
        class Tagger:
            def __init__(self): self.calls = []
            def ensure_tag(self, name):
                self.calls.append(("ensure", name))
                return {"name": name, "slug": "batch"}
            def add_tag(self, slug, tag):
                self.calls.append(("add", slug, tag["name"]))
                return "tagged"

        for force_extract in (False, True):
            with self.subTest(force_extract=force_extract), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                source = root / "scan.jpg"
                source.write_bytes(b"synthetic recipe source")
                digest = file_digest(source)
                cache = root / "extracted"
                cache.mkdir()
                candidate = RecipeCandidate(name="Toast", ingredients=["bread"], instructions=["Toast."])
                (cache / f"{digest}.json").write_text(
                    ExtractionBatch(recipes=[candidate]).model_dump_json(), encoding="utf-8")
                state_path = root / "import_state.jsonl"
                batch = ExtractionBatch(recipes=[candidate])
                reviews = ReviewStore(root / "review_events.jsonl")
                registration = reviews.register_candidates(
                    str(source), digest, extraction_identity(digest, batch.model_dump(mode="json")),
                    batch.recipes,
                )[0]
                identity = registration["candidate_id"]
                StateStore(state_path).add(
                    identity, "IMPORTED", "toast", str(source), candidate.name,
                    identity=identity, source_hash=digest, tag_status="failed",
                    tag_name="Batch", batch_tag="Batch",
                )
                state_before = state_path.read_bytes()
                event_path = root / "ui_import_events.jsonl"
                event_before = event_path.read_bytes() if event_path.exists() else None
                tagger = Tagger()
                extracted = ExtractionBatch(recipes=[candidate])

                with patch("import_recipes.extract", return_value=extracted) as extract_call, \
                        patch("import_recipes.import_mealie") as create_call:
                    rows = process_file(
                        source, digest, {"api_key": "", "model": "primary",
                                          "mealie_url": "", "mealie_token": ""},
                        None, StateStore(state_path), cache, True,
                        force_extract=force_extract, batch_tag="Batch", mealie_client=tagger,
                    )

                self.assertEqual(rows[0]["status"], "ALREADY_DONE", rows)
                self.assertEqual(rows[0]["candidate_id"], identity)
                self.assertEqual(tagger.calls, [])
                create_call.assert_not_called()
                self.assertEqual(extract_call.call_count, int(force_extract))
                self.assertEqual(state_path.read_bytes(), state_before)
                self.assertEqual(event_path.read_bytes() if event_path.exists() else None, event_before)
                self.assertTrue(any(item.get("candidate_id") == identity
                                    for item in ReviewStore(root / "review_events.jsonl").registrations.get(identity, [])))


if __name__ == "__main__":
    unittest.main()
