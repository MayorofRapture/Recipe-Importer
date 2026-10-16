import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from import_recipes import StateStore, file_digest, process_file
from recipe_quality import ExtractionBatch, RecipeCandidate


class CliSharedIntentTests(unittest.TestCase):
    def test_two_cli_instances_with_stale_history_share_one_creation_intent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "synthetic.jpg"
            source.write_bytes(b"synthetic scan")
            digest = file_digest(source)
            cache = root / "extracted"
            cache.mkdir()
            (cache / f"{digest}.json").write_text(ExtractionBatch(recipes=[RecipeCandidate(
                name="Synthetic rice", ingredients=["1 cup rice"], instructions=["Cook."],
            )]).model_dump_json(), encoding="utf-8")
            first_state = StateStore(root / "import_state.jsonl")
            second_state = StateStore(root / "import_state.jsonl")
            started, release = threading.Event(), threading.Event()
            creates = []

            def create(*_):
                creates.append("create")
                if len(creates) == 1:
                    started.set()
                    if not release.wait(10):
                        raise RuntimeError("test release was not delivered")
                return "synthetic-rice"

            def run(state):
                return process_file(source, digest, {"model": "fixture", "api_key": "", "mealie_url": "",
                                                    "mealie_token": ""}, None, state, cache, False)

            with patch("import_recipes.import_mealie", side_effect=create):
                with ThreadPoolExecutor(max_workers=2) as workers:
                    first = workers.submit(run, first_state)
                    self.assertTrue(started.wait(10))
                    try:
                        second = workers.submit(run, second_state).result(timeout=10)
                    finally:
                        release.set()
                    first_rows = first.result(timeout=10)
            self.assertEqual(creates, ["create"])
            self.assertEqual(first_rows[0]["status"], "IMPORTED")
            self.assertEqual(second[0]["status"], "NEEDS_MANUAL_CHECK")
