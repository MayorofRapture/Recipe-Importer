from __future__ import annotations

import tempfile
import unittest
from hashlib import sha256
from pathlib import Path
from unittest.mock import patch

from local_ocr import OcrResult
from model_routing import ModelConfig, ModelRouter, UsageLedger
from recipe_quality import ExtractionBatch, RecipeCandidate
from production_range_extractor import extract_range
from batch_store import BatchStore
from ui_job_service import JobService
import fitz


class ProductionRangeExtractorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.output = Path(self.temp.name) / "output"
        self.output.mkdir()
        self.source = Path(self.temp.name) / "range.pdf"
        self.source.write_bytes(b"synthetic canonical range")
        self.context = {"input_path": str(self.source),
                        "input_sha256": sha256(self.source.read_bytes()).hexdigest()}
        self.candidate = RecipeCandidate(name="Soup", ingredients=["1 cup water"],
                                         instructions=["Boil water."], requires_review=True)

    def tearDown(self):
        self.temp.cleanup()

    def test_local_ocr_returns_recipe_with_ocr_provenance_without_provider(self):
        ocr = OcrResult(text="source text", engine="fake", batch=ExtractionBatch(recipes=[self.candidate]),
                        confidence="medium", uncertainties=("check scan",))
        with patch("local_ocr.extract_local", return_value=ocr) as local, \
             patch("import_recipes.extract") as gemini:
            rows = extract_range(self.context, "local_ocr", {}, self.output)
        local.assert_called_once()
        gemini.assert_not_called()
        self.assertEqual(rows[0]["recipe"]["name"], "Soup")
        self.assertEqual(rows[0]["ocr"]["uncertainties"], ["check scan"])
        self.assertEqual(rows[0]["status"], "NEEDS_REVIEW")

    def test_gemini_internal_requests_each_reserve_once_through_hook(self):
        ledger = UsageLedger(self.output / "gemini_usage.json")
        router = ModelRouter([ModelConfig("model-a", rpd=3)], ledger, sleep=lambda _: None)
        calls = []

        def fake_extract(path, api_key, model, gate=None, before_request=None):
            before_request(model, 11)
            calls.append(model)
            before_request(model, 11)  # models a bounded retry inside import_recipes.extract
            calls.append(model)
            return ExtractionBatch(recipes=[self.candidate])

        with patch("import_recipes.extract", side_effect=fake_extract):
            rows = extract_range(self.context, "gemini", {"api_key": "test"}, self.output,
                                 router=router,
                                 before_provider_request=lambda model, tokens=0: ledger.reserve_scheduled(
                                     model, rpd=3, tpm=100, tokens=tokens, sleep=lambda _: None))
        self.assertEqual(calls, ["model-a", "model-a"])
        self.assertEqual(ledger.count("model-a"), 2)
        usage = __import__("json").loads((self.output / "gemini_usage.json").read_text())
        self.assertEqual(sum(item[1] for item in usage["model_token_windows"]["model-a"]), 22)
        self.assertEqual(rows[0]["model"], "model-a")

    def test_ocr_assisted_assembles_only_referenced_source_lines(self):
        ocr = OcrResult(text="Soup\n1 cup water\nBoil water.", engine="fake",
                        batch=ExtractionBatch(recipes=[self.candidate]), confidence="high")
        refs = type("Refs", (), {})()
        refs.recipes = []
        from ocr_structuring import OcrLineReferences, RecipeLineReferences, LineSpan
        refs = OcrLineReferences(recipes=[RecipeLineReferences(
            title=LineSpan(start=1, end=1), ingredients=[LineSpan(start=2, end=2)],
            instructions=[LineSpan(start=3, end=3)], confidence="high")])
        router = ModelRouter([ModelConfig("model-a", rpd=3)],
                             UsageLedger(self.output / "gemini_usage.json"), sleep=lambda _: None)
        with patch("local_ocr.extract_local", return_value=ocr), \
             patch("import_recipes.structure_ocr_text", return_value=refs) as structure:
            rows = extract_range(self.context, "ocr_assisted", {"api_key": "test"}, self.output,
                                 router=router, before_provider_request=lambda *_: None)
        structure.assert_called_once()
        self.assertEqual(rows[0]["recipe"]["ingredients"], ["1 cup water"])
        self.assertEqual(rows[0]["recipe"]["instructions"], ["Boil water."])
        self.assertEqual(rows[0]["extraction_method"], "ocr_assisted")

    def test_production_job_publishes_v2_cache_then_registers_range_candidate(self):
        batch = BatchStore(self.output).create("Production range fixture")
        parent_path = Path(self.temp.name) / "parent.pdf"
        document = fitz.open()
        document.new_page().insert_text((72, 72), "Recipe page")
        document.save(parent_path)
        document.close()
        parent = BatchStore(self.output).stage_parent_pdf(batch["batch_id"], parent_path)
        selected = BatchStore(self.output).create_page_range(
            batch["batch_id"], parent["document_id"], 1, 1)
        ocr = OcrResult(text="source text", engine="fake",
                        batch=ExtractionBatch(recipes=[self.candidate]), confidence="medium")
        with patch.dict("os.environ", {"RECIPE_IMPORTER_FIXTURE_MODE": "0"}), \
             patch("local_ocr.extract_local", return_value=ocr):
            service = JobService(self.output)
            job = service.start_range_job(batch["batch_id"], [selected["range_id"]], "local_ocr")
            self.assertEqual(job["execution_kind"], "production")
            completed = service.run_range_pending(job["job_id"])
        self.assertEqual(completed["status"], "completed")
        record = __import__("json").loads(Path(completed["ranges"][0]["cache_path"]).read_text())
        self.assertEqual(record["schema_version"], 2)
        self.assertEqual(record["kind"], "production_range_result")
        self.assertFalse((self.output / "extracted").exists())
        self.assertFalse((self.output / "import_state.jsonl").exists())
        self.assertTrue((self.output / "review_events.jsonl").exists())
        record["execution_kind"] = "synthetic"
        payload = {key: value for key, value in record.items() if key != "record_sha256"}
        record["record_sha256"] = sha256(__import__("json").dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        Path(completed["ranges"][0]["cache_path"]).write_text(
            __import__("json").dumps(record), encoding="utf-8")
        self.assertIsNone(service._find_job_range_cache(completed["ranges"][0], completed))

    def test_background_production_range_uses_durable_range_worker_entrypoint(self):
        batch = BatchStore(self.output).create("Detached range worker fixture")
        parent_path = Path(self.temp.name) / "worker-parent.pdf"
        document = fitz.open()
        document.new_page().insert_text((72, 72), "Recipe page")
        document.save(parent_path)
        document.close()
        parent = BatchStore(self.output).stage_parent_pdf(batch["batch_id"], parent_path)
        selected = BatchStore(self.output).create_page_range(
            batch["batch_id"], parent["document_id"], 1, 1)
        with patch.dict("os.environ", {"RECIPE_IMPORTER_FIXTURE_MODE": "0"}), \
             patch("ui_job_service.subprocess.Popen") as popen:
            job = JobService(self.output, background=True).start_range_job(
                batch["batch_id"], [selected["range_id"]], "local_ocr")
        self.assertEqual(job["execution_kind"], "production")
        self.assertIn("--range-job", popen.call_args.args[0])

    def test_cloud_range_rechecks_consent_and_pauses_at_daily_reservation(self):
        batch = BatchStore(self.output).create("Daily quota range fixture")
        parent_path = Path(self.temp.name) / "quota-parent.pdf"
        document = fitz.open()
        document.new_page().insert_text((72, 72), "Recipe page")
        document.save(parent_path)
        document.close()
        parent = BatchStore(self.output).stage_parent_pdf(batch["batch_id"], parent_path)
        selected = BatchStore(self.output).create_page_range(
            batch["batch_id"], parent["document_id"], 1, 1)
        config = {"api_key": "synthetic-key", "model": "model-a",
                  "models_json": '[{"id":"model-a","role":"primary","rpd":1}]',
                  "global_rpm": 60}
        UsageLedger(self.output / "gemini_usage.json").reserve("model-a", rpd=1)
        service = JobService(self.output, config=config)
        with patch.dict("os.environ", {"RECIPE_IMPORTER_FIXTURE_MODE": "0"}):
            job = service.start_range_job(batch["batch_id"], [selected["range_id"]], "gemini",
                                          cloud_consent=True)
            restored_config = JobService(self.output, config={
                "api_key": "runtime-only-key", "model": "changed-model",
                "models_json": '[{"id":"changed-model"}]', "global_rpm": 1,
            })._production_config("gemini", job["config"])
            self.assertEqual(restored_config["model"], "model-a")
            self.assertEqual(restored_config["api_key"], "runtime-only-key")

            def should_not_reach_provider(path, api_key, model, gate=None, before_request=None):
                before_request(model)
                raise AssertionError("Provider call continued after the daily quota reservation failed")

            with patch("import_recipes.extract", side_effect=should_not_reach_provider):
                paused = service.run_range_pending(job["job_id"])
        self.assertEqual(paused["status"], "quota_paused")
        self.assertEqual(paused["ranges"][0]["status"], "quota_paused")
        self.assertEqual(UsageLedger(self.output / "gemini_usage.json").count("model-a"), 1)


if __name__ == "__main__":
    unittest.main()
