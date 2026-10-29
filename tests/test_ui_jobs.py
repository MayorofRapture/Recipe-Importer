from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import fitz
from PIL import Image
import io

import ui_job_service
from batch_store import BatchStore, _file_lock
from fixture_extraction import create_fixture_extractor
from recipe_quality import ExtractionBatch, RecipeCandidate
from review_store import ReviewStore
from ui_job_service import JobService
from ui_data import load_catalog


def _png(color: tuple[int, int, int] = (10, 20, 30)) -> bytes:
    image = Image.new("RGB", (8, 8), color)
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _batch(store: BatchStore, name: str, *contents: bytes) -> dict:
    batch = store.create(name)
    for index, data in enumerate(contents, 1):
        store.stage(batch["batch_id"], f"source-{index}.png", data)
    return batch


def _write_parent_pdf(path: Path, text: str = "Synthetic parent page") -> None:
    document = fitz.open()
    document.new_page().insert_text((72, 72), text)
    document.save(path)
    document.close()


def _recipe_rows(source: dict, mode: str, config: dict):
    return [{
        "status": "EXTRACTED_ONLY", "title": f"Synthetic {source['original_filename']}",
        "candidate_id": f"candidate-{source['digest'][:8]}", "route_reason": mode,
    }]


def _stable_fingerprint_extractor(context, mode, config):
    def name():
        return "stable"
    return [{"name": name()}]


class JobServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.output = Path(self.tmp.name) / "output"
        self.store = BatchStore(self.output)
        self.fixture_env = patch.dict(os.environ, {"RECIPE_IMPORTER_FIXTURE_MODE": "1"})
        self.fixture_env.start()
        self.addCleanup(self.fixture_env.stop)
        self.addCleanup(self.tmp.cleanup)

    def test_parent_documents_are_never_job_sources(self):
        batch = self.store.create("Parent boundary")
        parent_path = Path(self.tmp.name) / "book.pdf"
        _write_parent_pdf(parent_path)
        parent = self.store.stage_parent_pdf(batch["batch_id"], parent_path)
        page_range = self.store.create_page_range(
            batch["batch_id"], parent["document_id"], 1, 1,
        )
        service = JobService(self.output, extractor=_recipe_rows)

        with self.assertRaisesRegex(ValueError, "accepted source file"):
            service.start(batch["batch_id"], "local_ocr")
        self.assertEqual(service.list_jobs(), [])

        scan = self.store.stage(batch["batch_id"], "recipe.png", _png())
        job = service.start(batch["batch_id"], "local_ocr")

        self.assertEqual(parent["status"], "staged")
        self.assertEqual(page_range["status"], "staged")
        self.assertEqual([source["source_id"] for source in job["sources"]], [scan["source_id"]])
        self.assertNotIn(parent["document_id"], {source.get("document_id") for source in job["sources"]})
        self.assertNotIn(page_range["range_id"], {source.get("range_id") for source in job["sources"]})

    def test_range_jobs_cache_synthetic_results_and_register_review_candidates(self):
        batch = self.store.create("Range synthetic boundary")
        parent_path = Path(self.tmp.name) / "range-book.pdf"
        _write_parent_pdf(parent_path)
        parent = self.store.stage_parent_pdf(batch["batch_id"], parent_path)
        page_range = self.store.create_page_range(batch["batch_id"], parent["document_id"], 1, 1)
        calls = []

        def extractor(context, mode, config):
            calls.append((context["range_id"], mode))
            self.assertTrue(Path(context["input_path"]).is_file())
            return [{"name": "Synthetic soup", "ingredients": ["1 cup water"]},
                    {"name": "Synthetic bread", "ingredients": ["2 cups flour"]}]

        service = JobService(self.output, config={"extractor_version": "fixture-v1"}, extractor=extractor)
        with patch("review_store.ReviewStore.register_candidates") as register:
            job = service.start_range_job(batch["batch_id"], [page_range["range_id"]], "local_ocr")
            completed = service.run_range_pending(job["job_id"])
        register.assert_not_called()
        repeated = JobService(self.output, config={"extractor_version": "fixture-v1"},
                              extractor=extractor).start_range_job(
                                  batch["batch_id"], [page_range["range_id"]], "local_ocr")

        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["ranges"][0]["status"], "completed")
        self.assertEqual(completed["ranges"][0]["result_count"], 2)
        self.assertTrue(completed["ranges"][0]["cache_path"])
        self.assertEqual(repeated["job_id"], job["job_id"])
        self.assertEqual(len(calls), 1)
        registrations = [
            registration for values in ReviewStore(self.output / "review_events.jsonl").registrations.values()
            for registration in values if registration.get("event") == "candidate_registration"
        ]
        self.assertEqual(len(registrations), 2)
        self.assertEqual({item["source_hash"] for item in registrations}, {parent["sha256"]})
        self.assertTrue(all(item.get("range_provenance") for item in registrations))
        self.assertFalse((self.output / "import_state.jsonl").exists())
        self.assertFalse((self.output / "extracted").exists())
        self.assertFalse((self.output / "ui_jobs" / "jobs.json").exists())

    def test_cloud_range_consent_is_required_and_survives_reload(self):
        batch = self.store.create("Cloud consent range")
        parent_path = Path(self.tmp.name) / "consent-book.pdf"
        _write_parent_pdf(parent_path)
        parent = self.store.stage_parent_pdf(batch["batch_id"], parent_path)
        page_range = self.store.create_page_range(batch["batch_id"], parent["document_id"], 1, 1)
        service = JobService(self.output, config={"extractor_version": "fixture-v1"},
                             extractor=_recipe_rows)
        with self.assertRaisesRegex(ValueError, "explicit consent"):
            service.start_range_job(batch["batch_id"], [page_range["range_id"]], "gemini")
        job = service.start_range_job(batch["batch_id"], [page_range["range_id"]], "gemini",
                                      cloud_consent=True)
        self.assertIs(job["cloud_consent"], True)
        self.assertTrue(job["cloud_consent_at"])
        restarted = JobService(self.output, config={"extractor_version": "fixture-v1"},
                               extractor=_recipe_rows)
        persisted = restarted.range_snapshot(job["job_id"])
        self.assertIs(persisted["cloud_consent"], True)
        self.assertEqual(persisted["cloud_consent_at"], job["cloud_consent_at"])
        restarted._mutate_range_job(job["job_id"], lambda value: value.update(cloud_consent=False))
        with self.assertRaisesRegex(ValueError, "consent"):
            restarted.run_range_pending(job["job_id"])

    def test_range_jobs_retry_only_failed_range_and_preserve_completed_cache(self):
        batch = self.store.create("Range retry")
        parent_path = Path(self.tmp.name) / "retry-book.pdf"
        _write_parent_pdf(parent_path)
        parent = self.store.stage_parent_pdf(batch["batch_id"], parent_path)
        first = self.store.create_page_range(batch["batch_id"], parent["document_id"], 1, 1)
        parent_path2 = Path(self.tmp.name) / "retry-book-2.pdf"
        _write_parent_pdf(parent_path2, "Synthetic retry parent page")
        parent2 = self.store.stage_parent_pdf(batch["batch_id"], parent_path2)
        second = self.store.create_page_range(batch["batch_id"], parent2["document_id"], 1, 1)
        calls = []
        failed_once = {second["range_id"]}

        def extractor(context, mode, config):
            calls.append(context["range_id"])
            if context["range_id"] in failed_once:
                failed_once.remove(context["range_id"])
                raise RuntimeError("synthetic transient failure")
            return [{"name": f"Recipe {context['range_id'][:6]}", "steps": ["mix"]}]

        service = JobService(self.output, config={"extractor_version": "fixture-v1"}, extractor=extractor)
        job = service.start_range_job(batch["batch_id"], [first["range_id"], second["range_id"]], "local_ocr")
        partial = service.run_range_pending(job["job_id"])
        self.assertEqual(partial["status"], "completed_with_errors")
        states = {item["range_id"]: item for item in partial["ranges"]}
        self.assertEqual(states[first["range_id"]]["status"], "completed")
        self.assertEqual(states[second["range_id"]]["status"], "failed")
        first_cache = states[first["range_id"]]["cache_path"]

        resumed = service.resume_range_job(job["job_id"])
        complete = service.run_range_pending(job["job_id"])

        self.assertEqual(resumed["status"], "queued")
        self.assertEqual(complete["status"], "completed")
        final_states = {item["range_id"]: item for item in complete["ranges"]}
        self.assertEqual(final_states[first["range_id"]]["cache_path"], first_cache)
        self.assertEqual(calls.count(first["range_id"]), 1)
        self.assertEqual(calls.count(second["range_id"]), 2)

    def test_range_jobs_fail_closed_without_injected_extractor_and_reject_corrupt_input(self):
        batch = self.store.create("Range validation")
        parent_path = Path(self.tmp.name) / "validation-book.pdf"
        _write_parent_pdf(parent_path)
        parent = self.store.stage_parent_pdf(batch["batch_id"], parent_path)
        page_range = self.store.create_page_range(batch["batch_id"], parent["document_id"], 1, 1)
        with self.assertRaisesRegex(RuntimeError, "synthetic extractor"):
            JobService(self.output).start_range_job(batch["batch_id"], [page_range["range_id"]], "local_ocr")
        input_path = Path(page_range["input_path"])
        input_path.write_bytes(b"not a pdf")
        with self.assertRaisesRegex(ValueError, "range input"):
            JobService(self.output, extractor=_recipe_rows).start_range_job(
                batch["batch_id"], [page_range["range_id"]], "local_ocr")

    def test_changed_range_extraction_config_uses_distinct_job_and_cache(self):
        batch = self.store.create("Range config identity")
        parent_path = Path(self.tmp.name) / "config-book.pdf"
        _write_parent_pdf(parent_path)
        parent = self.store.stage_parent_pdf(batch["batch_id"], parent_path)
        page_range = self.store.create_page_range(batch["batch_id"], parent["document_id"], 1, 1)
        calls = []

        def extractor(context, mode, config):
            calls.append(config["extractor_version"])
            return [{"name": "Synthetic versioned result"}]

        first_service = JobService(self.output, config={"extractor_version": "one"}, extractor=extractor)
        first = first_service.start_range_job(batch["batch_id"], [page_range["range_id"]], "local_ocr")
        first_done = first_service.run_range_pending(first["job_id"])
        second_service = JobService(self.output, config={"extractor_version": "two"}, extractor=extractor)
        second = second_service.start_range_job(batch["batch_id"], [page_range["range_id"]], "local_ocr")
        second_done = second_service.run_range_pending(second["job_id"])

        self.assertNotEqual(first["job_id"], second["job_id"])
        self.assertNotEqual(first_done["ranges"][0]["cache_path"], second_done["ranges"][0]["cache_path"])
        self.assertEqual(calls, ["one", "two"])

    def test_range_cache_does_not_cross_unversioned_closure_instances(self):
        batch = self.store.create("Range closure identity")
        parent_path = Path(self.tmp.name) / "closure-book.pdf"
        _write_parent_pdf(parent_path)
        parent = self.store.stage_parent_pdf(batch["batch_id"], parent_path)
        page_range = self.store.create_page_range(batch["batch_id"], parent["document_id"], 1, 1)

        def make_extractor(value):
            def extractor(context, mode, config):
                return [{"name": value}]
            return extractor

        first_service = JobService(self.output, extractor=make_extractor("first"))
        first = first_service.start_range_job(batch["batch_id"], [page_range["range_id"]], "local_ocr")
        first_done = first_service.run_range_pending(first["job_id"])
        second_service = JobService(self.output, extractor=make_extractor("second"))
        second = second_service.start_range_job(batch["batch_id"], [page_range["range_id"]], "local_ocr")
        second_done = second_service.run_range_pending(second["job_id"])

        first_record = json.loads(Path(first_done["ranges"][0]["cache_path"]).read_text(encoding="utf-8"))
        second_record = json.loads(Path(second_done["ranges"][0]["cache_path"]).read_text(encoding="utf-8"))
        self.assertNotEqual(first["job_id"], second["job_id"])
        self.assertEqual(first_record["results"][0]["name"], "first")
        self.assertEqual(second_record["results"][0]["name"], "second")

    def test_explicit_range_extractor_version_reuses_same_config_and_separates_upgrades(self):
        batch = self.store.create("Range explicit version")
        parent_path = Path(self.tmp.name) / "explicit-version-book.pdf"
        _write_parent_pdf(parent_path)
        parent = self.store.stage_parent_pdf(batch["batch_id"], parent_path)
        page_range = self.store.create_page_range(batch["batch_id"], parent["document_id"], 1, 1)
        calls = []

        def extractor(context, mode, config):
            calls.append(config["extractor_version"])
            return [{"name": config["extractor_version"]}]

        one = JobService(self.output, {"extractor_version": "v1"}, extractor)
        first = one.start_range_job(batch["batch_id"], [page_range["range_id"]], "local_ocr")
        first_done = one.run_range_pending(first["job_id"])
        restarted = JobService(self.output, {"extractor_version": "v1"}, extractor)
        resumed = restarted.resume_range_job(first["job_id"])
        same = JobService(self.output, {"extractor_version": "v1"}, extractor)
        reused = same.start_range_job(batch["batch_id"], [page_range["range_id"]], "local_ocr")
        two = JobService(self.output, {"extractor_version": "v2"}, extractor)
        upgraded = two.start_range_job(batch["batch_id"], [page_range["range_id"]], "local_ocr")
        upgraded_done = two.run_range_pending(upgraded["job_id"])

        self.assertEqual(reused["job_id"], first["job_id"])
        self.assertEqual(resumed["status"], "completed")
        self.assertNotEqual(first_done["ranges"][0]["cache_path"], upgraded_done["ranges"][0]["cache_path"])
        self.assertEqual(calls, ["v1", "v2"])

    def test_unsupported_range_config_value_fails_closed(self):
        batch = self.store.create("Range unsupported config")
        parent_path = Path(self.tmp.name) / "unsupported-config-book.pdf"
        _write_parent_pdf(parent_path)
        parent = self.store.stage_parent_pdf(batch["batch_id"], parent_path)
        page_range = self.store.create_page_range(batch["batch_id"], parent["document_id"], 1, 1)

        with self.assertRaisesRegex(ValueError, "safely fingerprinted"):
            JobService(self.output, {"extractor_version": "v1", "effective_mode": object()}, _recipe_rows).start_range_job(
                batch["batch_id"], [page_range["range_id"]], "local_ocr")

    def test_same_range_extractor_version_with_different_config_does_not_reuse_cache(self):
        batch = self.store.create("Range config values")
        parent_path = Path(self.tmp.name) / "config-values-book.pdf"
        _write_parent_pdf(parent_path)
        parent = self.store.stage_parent_pdf(batch["batch_id"], parent_path)
        page_range = self.store.create_page_range(batch["batch_id"], parent["document_id"], 1, 1)
        calls = []

        def extractor(context, mode, config):
            calls.append(config["effective_setting"])
            return [{"name": config["effective_setting"]}]

        first_service = JobService(self.output, {"extractor_version": "v1", "effective_setting": "A"}, extractor)
        first = first_service.start_range_job(batch["batch_id"], [page_range["range_id"]], "local_ocr")
        first_done = first_service.run_range_pending(first["job_id"])
        second_service = JobService(self.output, {"extractor_version": "v1", "effective_setting": "B"}, extractor)
        second = second_service.start_range_job(batch["batch_id"], [page_range["range_id"]], "local_ocr")
        second_done = second_service.run_range_pending(second["job_id"])

        self.assertNotEqual(first["job_id"], second["job_id"])
        self.assertNotEqual(first_done["ranges"][0]["cache_path"], second_done["ranges"][0]["cache_path"])
        self.assertEqual(calls, ["A", "B"])

    def test_range_job_refuses_runtime_config_different_from_persisted_fingerprint(self):
        batch = self.store.create("Range runtime config")
        parent_path = Path(self.tmp.name) / "runtime-config-book.pdf"
        _write_parent_pdf(parent_path)
        parent = self.store.stage_parent_pdf(batch["batch_id"], parent_path)
        page_range = self.store.create_page_range(batch["batch_id"], parent["document_id"], 1, 1)
        calls = []

        def extractor(context, mode, config):
            calls.append(config.get("extractor_version"))
            return [{"name": "Synthetic mismatched result"}]

        created = JobService(self.output, config={"extractor_version": "config-a"}, extractor=extractor)
        job = created.start_range_job(batch["batch_id"], [page_range["range_id"]], "local_ocr")
        changed = JobService(self.output, config={"extractor_version": "config-b"}, extractor=extractor)

        with self.assertRaisesRegex(ValueError, "fingerprint"):
            changed.run_range_pending(job["job_id"])
        self.assertEqual(calls, [])

    def test_range_extractor_fingerprint_is_stable_across_processes(self):
        import sys

        local = ui_job_service._range_config_snapshot(
            {}, _stable_fingerprint_extractor)["extractor_implementation_sha256"]
        script = (
            "from ui_job_service import _range_config_snapshot; "
            "from tests.test_ui_jobs import _stable_fingerprint_extractor; "
            "print(_range_config_snapshot({}, _stable_fingerprint_extractor)['extractor_implementation_sha256'])"
        )
        external = subprocess.check_output([sys.executable, "-c", script], text=True).strip().splitlines()[-1]

        self.assertEqual(local, external)

    def test_range_extraction_failure_rows_are_failed_and_never_cached(self):
        batch = self.store.create("Range extraction failure")
        parent_path = Path(self.tmp.name) / "failed-range-book.pdf"
        _write_parent_pdf(parent_path)
        parent = self.store.stage_parent_pdf(batch["batch_id"], parent_path)
        page_range = self.store.create_page_range(batch["batch_id"], parent["document_id"], 1, 1)
        service = JobService(self.output, extractor=lambda context, mode, config: [{
            "status": "EXTRACTION_FAILED", "note": "Synthetic extraction failed.",
        }])
        job = service.start_range_job(batch["batch_id"], [page_range["range_id"]], "local_ocr")

        done = service.run_range_pending(job["job_id"])

        self.assertEqual(done["status"], "completed_with_errors")
        self.assertEqual(done["ranges"][0]["status"], "failed")
        self.assertEqual(done["ranges"][0]["cache_path"], "")

    def test_concurrent_range_job_starts_share_one_durable_job(self):
        batch = self.store.create("Range concurrent starts")
        parent_path = Path(self.tmp.name) / "concurrent-book.pdf"
        _write_parent_pdf(parent_path)
        parent = self.store.stage_parent_pdf(batch["batch_id"], parent_path)
        page_range = self.store.create_page_range(batch["batch_id"], parent["document_id"], 1, 1)

        def start(_):
            return JobService(self.output, config={"extractor_version": "fixture-v1"},
                              extractor=_recipe_rows).start_range_job(
                                  batch["batch_id"], [page_range["range_id"]], "local_ocr")["job_id"]

        with ThreadPoolExecutor(max_workers=6) as executor:
            job_ids = list(executor.map(start, range(6)))

        self.assertEqual(len(set(job_ids)), 1)
        self.assertEqual(len(JobService(self.output, extractor=_recipe_rows).list_range_jobs()), 1)

    def test_range_resume_recovers_interrupted_attempt_without_repeating_completed_range(self):
        batch = self.store.create("Range interrupted")
        parent_path = Path(self.tmp.name) / "interrupted-book.pdf"
        _write_parent_pdf(parent_path)
        parent = self.store.stage_parent_pdf(batch["batch_id"], parent_path)
        first = self.store.create_page_range(batch["batch_id"], parent["document_id"], 1, 1)
        parent_path2 = Path(self.tmp.name) / "interrupted-book-2.pdf"
        _write_parent_pdf(parent_path2, "Synthetic second parent page")
        parent2 = self.store.stage_parent_pdf(batch["batch_id"], parent_path2)
        second = self.store.create_page_range(batch["batch_id"], parent2["document_id"], 1, 1)
        calls = []

        def extractor(context, mode, config):
            calls.append(context["range_id"])
            if context["range_id"] == second["range_id"] and calls.count(second["range_id"]) == 1:
                raise RuntimeError("synthetic interruption precursor")
            return [{"name": "Synthetic recovered recipe"}]

        service = JobService(self.output, extractor=extractor)
        job = service.start_range_job(batch["batch_id"], [first["range_id"], second["range_id"]], "local_ocr")
        partial = service.run_range_pending(job["job_id"])
        self.assertEqual(partial["status"], "completed_with_errors")
        with _file_lock(service.range_manifest_lock):
            manifest = service._read_range_manifest()
            target = manifest["jobs"][job["job_id"]]
            interrupted = next(item for item in target["ranges"] if item["range_id"] == second["range_id"])
            interrupted["status"] = "processing"
            interrupted["attempts"].append({"attempt": 2, "status": "processing", "started_at": "synthetic"})
            target["status"] = "running"
            service._write_range_manifest(manifest)

        resumed = service.resume_range_job(job["job_id"])
        done = service.run_range_pending(job["job_id"])
        states = {item["range_id"]: item for item in done["ranges"]}

        resumed_states = {item["range_id"]: item for item in resumed["ranges"]}
        self.assertEqual(resumed_states[second["range_id"]]["attempts"][-1]["status"], "interrupted")
        self.assertEqual(states[second["range_id"]]["attempts"][-1]["status"], "completed")
        self.assertEqual(calls.count(first["range_id"]), 1)
        self.assertEqual(calls.count(second["range_id"]), 2)

    def test_range_pause_and_quota_resume_preserve_completed_work(self):
        batch = self.store.create("Range pause quota")
        parent_path = Path(self.tmp.name) / "pause-book.pdf"
        _write_parent_pdf(parent_path)
        parent = self.store.stage_parent_pdf(batch["batch_id"], parent_path)
        first = self.store.create_page_range(batch["batch_id"], parent["document_id"], 1, 1)
        parent_path2 = Path(self.tmp.name) / "pause-book-2.pdf"
        _write_parent_pdf(parent_path2, "Synthetic second pause page")
        parent2 = self.store.stage_parent_pdf(batch["batch_id"], parent_path2)
        second = self.store.create_page_range(batch["batch_id"], parent2["document_id"], 1, 1)
        service = None
        job = None
        calls = []

        def pausing_extractor(context, mode, config):
            calls.append(context["range_id"])
            if len(calls) == 1:
                service.pause_range_job(job["job_id"])
            return [{"name": "Synthetic paused result"}]

        service = JobService(self.output, extractor=pausing_extractor)
        job = service.start_range_job(batch["batch_id"], [first["range_id"], second["range_id"]], "local_ocr")
        paused = service.run_range_pending(job["job_id"])
        self.assertEqual(paused["status"], "paused")
        resumed = service.resume_range_job(job["job_id"])
        done = service.run_range_pending(job["job_id"])
        self.assertEqual(resumed["status"], "queued")
        self.assertEqual(done["status"], "completed")
        self.assertEqual(calls.count(first["range_id"]), 1)
        self.assertEqual(calls.count(second["range_id"]), 1)

        quota_batch = self.store.create("Range quota")
        quota_parent_path = Path(self.tmp.name) / "quota-book.pdf"
        _write_parent_pdf(quota_parent_path, "Synthetic quota parent page")
        quota_doc = fitz.open(quota_parent_path)
        quota_doc.new_page().insert_text((72, 72), "Synthetic quota second page")
        quota_two_page_path = Path(self.tmp.name) / "quota-book-two-pages.pdf"
        quota_doc.save(quota_two_page_path)
        quota_doc.close()
        quota_parent = self.store.stage_parent_pdf(quota_batch["batch_id"], quota_two_page_path)
        quota_range = self.store.create_page_range(quota_batch["batch_id"], quota_parent["document_id"], 1, 1)
        quota_range_two = self.store.create_page_range(quota_batch["batch_id"], quota_parent["document_id"], 2, 2)
        quota_calls = []

        def quota_extractor(context, mode, config):
            quota_calls.append(1)
            if len(quota_calls) == 1:
                return [{"status": "QUOTA_EXHAUSTED", "note": "Synthetic quota pause"}]
            return [{"name": "Synthetic after quota"}]

        quota_service = JobService(self.output, extractor=quota_extractor)
        quota_job = quota_service.start_range_job(
            quota_batch["batch_id"], [quota_range["range_id"], quota_range_two["range_id"]], "gemini",
            cloud_consent=True)
        quota_paused = quota_service.run_range_pending(quota_job["job_id"])
        self.assertEqual(quota_paused["status"], "quota_paused")
        still_paused = quota_service.run_range_pending(quota_job["job_id"])
        self.assertEqual(still_paused["status"], "quota_paused")
        self.assertEqual(len(quota_calls), 1)
        quota_service.resume_range_job(quota_job["job_id"])
        quota_done = quota_service.run_range_pending(quota_job["job_id"])
        self.assertEqual(quota_done["status"], "completed")
        self.assertEqual(len(quota_calls), 3)

    def test_missing_or_corrupt_range_cache_reprocesses_only_after_new_start(self):
        batch = self.store.create("Range cache integrity")
        parent_path = Path(self.tmp.name) / "cache-book.pdf"
        _write_parent_pdf(parent_path)
        parent = self.store.stage_parent_pdf(batch["batch_id"], parent_path)
        page_range = self.store.create_page_range(batch["batch_id"], parent["document_id"], 1, 1)
        calls = []

        def extractor(context, mode, config):
            calls.append(1)
            return [{"name": "Synthetic cache result", "candidate_id": "must-not-persist"}]

        service = JobService(self.output, config={"api_key": "private-test-key", "extractor_version": "fixture-v1"}, extractor=extractor)
        first = service.start_range_job(batch["batch_id"], [page_range["range_id"]], "local_ocr")
        completed = service.run_range_pending(first["job_id"])
        cache_path = Path(completed["ranges"][0]["cache_path"])
        record = json.loads(cache_path.read_text(encoding="utf-8"))
        self.assertNotIn("candidate_id", record["results"][0])
        self.assertNotIn("private-test-key", service.range_manifest_path.read_text(encoding="utf-8"))

        cache_path.write_text("{broken", encoding="utf-8")
        second = JobService(self.output, config={"api_key": "private-test-key", "extractor_version": "fixture-v1"}, extractor=extractor).start_range_job(
            batch["batch_id"], [page_range["range_id"]], "local_ocr")
        self.assertNotEqual(first["job_id"], second["job_id"])
        rerun = JobService(self.output, config={"api_key": "private-test-key", "extractor_version": "fixture-v1"}, extractor=extractor).run_range_pending(
            second["job_id"])
        self.assertEqual(rerun["status"], "completed")
        self.assertEqual(len(calls), 2)

    def test_valid_checksum_cannot_authorize_contradictory_range_provenance(self):
        batch = self.store.create("Range provenance integrity")
        parent_path = Path(self.tmp.name) / "provenance-book.pdf"
        _write_parent_pdf(parent_path)
        parent = self.store.stage_parent_pdf(batch["batch_id"], parent_path)
        page_range = self.store.create_page_range(batch["batch_id"], parent["document_id"], 1, 1)
        calls = []

        def extractor(context, mode, config):
            calls.append(context["range_id"])
            return [{"name": "Synthetic provenance result"}]

        def resign(record):
            payload = {key: value for key, value in record.items() if key != "record_sha256"}
            canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            record["record_sha256"] = ui_job_service.sha256(canonical.encode("utf-8")).hexdigest()

        service = JobService(self.output, {"extractor_version": "v1"}, extractor)
        job = service.start_range_job(batch["batch_id"], [page_range["range_id"]], "local_ocr")
        done = service.run_range_pending(job["job_id"])
        mutations = {
            "schema_version": 2,
            "kind": "other_record",
            "batch_id": "other-batch",
            "parent_document_id": "other-parent",
            "parent_sha256": "0" * 64,
            "range_id": "other-range",
            "first_page": 9,
            "last_page": 9,
            "input_sha256": "1" * 64,
            "mode": "gemini",
            "config_fingerprint": "2" * 64,
            "cache_identity": "3" * 64,
        }

        for field, value in mutations.items():
            with self.subTest(field=field):
                cache_path = Path(done["ranges"][0]["cache_path"])
                record = json.loads(cache_path.read_text(encoding="utf-8"))
                record[field] = value
                resign(record)
                cache_path.write_text(json.dumps(record), encoding="utf-8")

                retry_service = JobService(self.output, {"extractor_version": "v1"}, extractor)
                retry = retry_service.start_range_job(batch["batch_id"], [page_range["range_id"]], "local_ocr")
                retried = retry_service.run_range_pending(retry["job_id"])

                self.assertNotEqual(retry["job_id"], job["job_id"])
                self.assertEqual(retried["status"], "completed")
                self.assertEqual(len(calls), 2 + list(mutations).index(field))
                job, done = retry, retried

    def test_range_cache_checksum_mismatch_is_rejected(self):
        batch = self.store.create("Range checksum integrity")
        parent_path = Path(self.tmp.name) / "checksum-book.pdf"
        _write_parent_pdf(parent_path)
        parent = self.store.stage_parent_pdf(batch["batch_id"], parent_path)
        page_range = self.store.create_page_range(batch["batch_id"], parent["document_id"], 1, 1)
        calls = []

        def extractor(context, mode, config):
            calls.append(1)
            return [{"name": "Synthetic checksum result"}]

        service = JobService(self.output, {"extractor_version": "v1"}, extractor)
        first = service.start_range_job(batch["batch_id"], [page_range["range_id"]], "local_ocr")
        done = service.run_range_pending(first["job_id"])
        cache_path = Path(done["ranges"][0]["cache_path"])
        record = json.loads(cache_path.read_text(encoding="utf-8"))
        record["results"][0]["name"] = "tampered without updating checksum"
        cache_path.write_text(json.dumps(record), encoding="utf-8")

        retry_service = JobService(self.output, {"extractor_version": "v1"}, extractor)
        retry = retry_service.start_range_job(batch["batch_id"], [page_range["range_id"]], "local_ocr")
        retried = retry_service.run_range_pending(retry["job_id"])

        self.assertNotEqual(first["job_id"], retry["job_id"])
        self.assertEqual(retried["status"], "completed")
        self.assertEqual(len(calls), 2)

    def test_range_cache_rejects_unowned_filename(self):
        batch = self.store.create("Range cache ownership")
        parent_path = Path(self.tmp.name) / "ownership-book.pdf"
        _write_parent_pdf(parent_path)
        parent = self.store.stage_parent_pdf(batch["batch_id"], parent_path)
        page_range = self.store.create_page_range(batch["batch_id"], parent["document_id"], 1, 1)
        calls = []

        def extractor(context, mode, config):
            calls.append(1)
            return [{"name": "Synthetic owned result"}]

        service = JobService(self.output, {"extractor_version": "v1"}, extractor)
        first = service.start_range_job(batch["batch_id"], [page_range["range_id"]], "local_ocr")
        done = service.run_range_pending(first["job_id"])
        cache_path = Path(done["ranges"][0]["cache_path"])
        record = json.loads(cache_path.read_text(encoding="utf-8"))
        unowned_path = cache_path.with_name(f"{record['cache_identity']}--unowned.json")
        cache_path.rename(unowned_path)

        retry_service = JobService(self.output, {"extractor_version": "v1"}, extractor)
        retry = retry_service.start_range_job(batch["batch_id"], [page_range["range_id"]], "local_ocr")
        retried = retry_service.run_range_pending(retry["job_id"])

        self.assertNotEqual(first["job_id"], retry["job_id"])
        self.assertEqual(retried["status"], "completed")
        self.assertEqual(len(calls), 2)

    def test_range_no_recipe_is_durable_and_cached_as_no_recipe(self):
        batch = self.store.create("Range no recipe")
        parent_path = Path(self.tmp.name) / "no-recipe-book.pdf"
        _write_parent_pdf(parent_path, "Synthetic first page")
        doc = fitz.open(parent_path)
        doc.new_page().insert_text((72, 72), "Synthetic second page")
        replacement = Path(self.tmp.name) / "no-recipe-book-two-pages.pdf"
        doc.save(replacement)
        doc.close()
        parent_path = replacement
        parent = self.store.stage_parent_pdf(batch["batch_id"], parent_path)
        page_range = self.store.create_page_range(batch["batch_id"], parent["document_id"], 1, 1)
        recipe_range = self.store.create_page_range(batch["batch_id"], parent["document_id"], 2, 2)
        calls = []

        def extractor(context, mode, config):
            calls.append(context["range_id"])
            if context["range_id"] == page_range["range_id"]:
                return [{"status": "NO_RECIPE", "note": "No recipe in synthetic range."}]
            return [{"name": "Synthetic recipe on second page"}]

        service = JobService(self.output, config={"extractor_version": "fixture-v1"}, extractor=extractor)
        job = service.start_range_job(batch["batch_id"],
                                      [page_range["range_id"], recipe_range["range_id"]], "local_ocr")
        queued_subset = JobService(self.output, config={"extractor_version": "fixture-v1"}, extractor=extractor).start_range_job(
            batch["batch_id"], [page_range["range_id"]], "local_ocr")
        done = service.run_range_pending(job["job_id"])
        cached = JobService(self.output, config={"extractor_version": "fixture-v1"}, extractor=extractor).run_range_pending(queued_subset["job_id"])

        self.assertEqual({item["range_id"]: item["status"] for item in done["ranges"]}[page_range["range_id"]], "no_recipe")
        self.assertEqual(cached["ranges"][0]["status"], "no_recipe")
        self.assertEqual(calls.count(page_range["range_id"]), 1)

    def test_start_is_durable_and_second_tab_returns_same_nonterminal_job(self):
        batch = _batch(self.store, "Weekend", _png())
        service = JobService(self.output, extractor=_recipe_rows)

        first = service.start(batch["batch_id"], "local_ocr")
        second = JobService(self.output, extractor=_recipe_rows).start(batch["batch_id"], "local_ocr")

        self.assertEqual(first["status"], "queued")
        self.assertEqual(second["job_id"], first["job_id"])
        self.assertEqual(JobService(self.output, extractor=_recipe_rows).snapshot(first["job_id"]), first)

    def test_cloud_modes_require_consent_and_fixture_mode_requires_injected_fake(self):
        batch = _batch(self.store, "Disclosure", _png())
        service = JobService(self.output, extractor=_recipe_rows)

        with self.assertRaisesRegex(ValueError, "consent"):
            service.start(batch["batch_id"], "gemini")
        with self.assertRaisesRegex(ValueError, "consent"):
            service.start(batch["batch_id"], "ocr_assisted")
        with self.assertRaisesRegex(RuntimeError, "[Ff]ixture mode"):
            JobService(self.output).start(batch["batch_id"], "local_ocr")

        gemini = service.start(batch["batch_id"], "gemini", cloud_consent=True)
        self.assertEqual(gemini["cloud_consent"], True)
        self.assertIsNotNone(gemini["cloud_consent_at"])

    def test_synthetic_extractor_requires_marker_and_emits_non_importable_placeholder(self):
        with self.assertRaisesRegex(RuntimeError, "marked demonstration"):
            create_fixture_extractor(self.output)
        self.output.mkdir(parents=True, exist_ok=True)
        (self.output / ".fixture-library").write_text("synthetic test only", encoding="utf-8")
        extract = create_fixture_extractor(self.output)
        result = extract({"original_filename": "recipe.png"}, "local_ocr", {})

        self.assertEqual(result.recipes[0].name, "Synthetic demo from recipe")
        self.assertTrue(result.recipes[0].requires_review)
        self.assertEqual(result.recipes[0].ingredients, ["1 synthetic cup of flour"])
        self.assertIn("Synthetic demonstration content only", result.recipes[0].warnings[0])

    def test_rejects_unknown_modes_without_creating_a_job(self):
        batch = _batch(self.store, "Mode", _png())
        service = JobService(self.output, extractor=_recipe_rows)

        with self.assertRaisesRegex(ValueError, "mode"):
            service.start(batch["batch_id"], "surprise")
        self.assertEqual(service.list_jobs(), [])

    def test_real_cloud_job_requires_configuration_before_persisting_or_launching(self):
        batch = _batch(self.store, "No key", _png())
        with patch.dict(os.environ, {"RECIPE_IMPORTER_FIXTURE_MODE": "0"}, clear=False), \
             patch.dict(os.environ, {"GEMINI_API_KEY": ""}):
            service = JobService(self.output, config={})
            with self.assertRaisesRegex(ValueError, "GEMINI_API_KEY"):
                service.start(batch["batch_id"], "gemini", cloud_consent=True)
        self.assertEqual(service.list_jobs(), [])

    def test_run_pending_publishes_recipe_counts_and_completed_source_results(self):
        batch = _batch(self.store, "Counts", _png())
        service = JobService(self.output, extractor=_recipe_rows)
        job = service.start(batch["batch_id"], "local_ocr")

        snapshot = service.run_pending(job["job_id"])

        self.assertEqual(snapshot["status"], "completed")
        self.assertEqual(snapshot["progress"], {"total": 1, "completed": 1, "remaining": 0})
        self.assertEqual(snapshot["recipe_count"], 1)
        self.assertEqual(snapshot["candidate_count"], 1)
        self.assertEqual(snapshot["sources"][0]["status"], "completed")
        self.assertEqual(snapshot["sources"][0]["results"][0]["title"], "Synthetic source-1.png")
        self.assertEqual(service.list_jobs()[0]["job_id"], job["job_id"])

    def test_new_sources_added_after_completed_job_get_a_new_job_without_reprocessing_successes(self):
        batch = _batch(self.store, "Append after completion", _png())
        calls = []

        def extractor(source, mode, config):
            calls.append(source["source_id"])
            return _recipe_rows(source, mode, config)

        service = JobService(self.output, extractor=extractor)
        first = service.start(batch["batch_id"], "local_ocr")
        first_done = service.run_pending(first["job_id"])
        appended = self.store.stage(batch["batch_id"], "source-2.png", _png((30, 40, 50)))

        second = service.start(batch["batch_id"], "local_ocr")

        self.assertEqual(first_done["status"], "completed")
        self.assertNotEqual(second["job_id"], first["job_id"])
        self.assertEqual([source["source_id"] for source in second["sources"]], [appended["source_id"]])
        second_done = service.run_pending(second["job_id"])
        self.assertEqual(second_done["status"], "completed")
        self.assertEqual(calls, [first["sources"][0]["source_id"], appended["source_id"]])

    def test_extraction_batch_is_published_as_versioned_review_catalog_cache(self):
        batch = _batch(self.store, "Catalog handoff", _png())
        candidate = RecipeCandidate(
            name="Synthetic review candidate", ingredients=["1 cup test flour"],
            instructions=["Mix the synthetic fixture."], requires_review=True,
        )
        service = JobService(self.output, extractor=lambda source, mode, config: ExtractionBatch(recipes=[candidate]))
        job = service.start(batch["batch_id"], "local_ocr")

        snapshot = service.run_pending(job["job_id"])
        views = load_catalog(self.output)

        self.assertEqual(snapshot["recipe_count"], 1)
        self.assertTrue(any(view.candidate.name == candidate.name for view in views))
        view = next(view for view in views if view.candidate.name == candidate.name)
        self.assertEqual(view.source_hash, snapshot["sources"][0]["digest"])
        self.assertTrue(view.source_available)
        self.assertEqual(Path(view.source_path), Path(snapshot["sources"][0]["staged_path"]))
        self.assertEqual(len(list((self.output / "extracted").glob("*.json"))), 1)

    def test_local_ocr_adapter_registers_candidate_and_publishes_review_cache(self):
        batch = _batch(self.store, "Local OCR adapter", _png())
        candidate = RecipeCandidate(
            name="Synthetic OCR candidate", ingredients=["2 test cups"],
            instructions=["Stir the synthetic test."], requires_review=True,
            source_method="python", confidence="low",
        )

        def fake_ocr(path, digest, output_dir, tesseract_cmd, reviews):
            candidate_id = f"{digest}:synthetic"
            reviews.add_candidate(str(path), candidate_id, candidate)
            return [{"status": "OCR_NEEDS_REVIEW", "title": candidate.name, "candidate_id": candidate_id}]

        with patch.dict(os.environ, {"RECIPE_IMPORTER_FIXTURE_MODE": "0"}):
            service = JobService(self.output, config={})
            with patch.object(service, "_launch_worker"), patch("import_recipes._ocr_only_file", side_effect=fake_ocr):
                job = service.start(batch["batch_id"], "local_ocr")
                snapshot = service.run_pending(job["job_id"])

        views = load_catalog(self.output)
        view = next(item for item in views if item.candidate.name == candidate.name)
        self.assertEqual(snapshot["status"], "completed")
        self.assertEqual(snapshot["candidate_count"], 1)
        self.assertEqual(view.source_hash, snapshot["sources"][0]["digest"])
        self.assertTrue(view.source_available)

    def test_zero_recipe_result_is_distinct_from_extraction_failure(self):
        batch = _batch(self.store, "No recipe", _png())
        service = JobService(
            self.output,
            extractor=lambda source, mode, config: [{"status": "NO_RECIPE", "title": "", "note": "No recipe detected."}],
        )
        job = service.start(batch["batch_id"], "local_ocr")

        snapshot = service.run_pending(job["job_id"])

        self.assertEqual(snapshot["status"], "completed")
        self.assertEqual(snapshot["recipe_count"], 0)
        self.assertEqual(snapshot["failure_count"], 0)
        self.assertEqual(snapshot["sources"][0]["status"], "no_recipe")
        self.assertEqual(snapshot["sources"][0]["results"][0]["status"], "NO_RECIPE")

    def test_quota_result_pauses_job_and_is_not_counted_as_source_failure(self):
        batch = _batch(self.store, "Quota", _png())
        service = JobService(
            self.output,
            extractor=lambda source, mode, config: [{
                "status": "EXTRACTION_FAILED", "route_reason": "quota_fallback",
                "note": "All configured models are quota exhausted.",
            }],
        )
        job = service.start(batch["batch_id"], "gemini", cloud_consent=True)

        snapshot = service.run_pending(job["job_id"])

        self.assertEqual(snapshot["status"], "quota_paused")
        self.assertEqual(snapshot["quota_paused"], True)
        self.assertEqual(snapshot["failure_count"], 0)
        self.assertEqual(snapshot["sources"][0]["status"], "quota_paused")

    def test_pause_after_current_file_and_resume_does_not_repeat_successful_source(self):
        batch = _batch(self.store, "Pause", _png((1, 2, 3)), _png((4, 5, 6)))
        calls: list[str] = []
        service = None
        job = None

        def pause_during_first(source, mode, config):
            calls.append(source["source_id"])
            if len(calls) == 1:
                service.pause(job["job_id"])
            return _recipe_rows(source, mode, config)

        service = JobService(self.output, extractor=pause_during_first)
        job = service.start(batch["batch_id"], "local_ocr")
        paused = service.run_pending(job["job_id"])

        self.assertEqual(paused["status"], "paused")
        self.assertEqual(paused["progress"], {"total": 2, "completed": 1, "remaining": 1})
        self.assertEqual(paused["sources"][0]["status"], "completed")
        self.assertEqual(paused["sources"][1]["status"], "waiting")

        restarted = JobService(self.output, extractor=pause_during_first)
        resumed = restarted.resume(job["job_id"])
        completed = restarted.run_pending(job["job_id"])

        self.assertEqual(resumed["status"], "queued")
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0], paused["sources"][0]["source_id"])
        self.assertEqual(calls[1], paused["sources"][1]["source_id"])

    def test_resume_recovers_source_left_processing_by_a_stopped_worker(self):
        batch = _batch(self.store, "Restart", _png())
        service = JobService(self.output, extractor=_recipe_rows)
        job = service.start(batch["batch_id"], "local_ocr")

        def simulate_crash(current):
            source = current["sources"][0]
            source["status"] = "processing"
            source["attempt_count"] = 1
            source["attempts"].append({"attempt": 1, "status": "processing"})
            current["status"] = "running"
            current["current_source_id"] = source["source_id"]

        service._mutate_job(job["job_id"], simulate_crash)
        recovered = JobService(self.output, extractor=_recipe_rows).resume(job["job_id"])

        self.assertEqual(recovered["status"], "queued")
        self.assertEqual(recovered["sources"][0]["status"], "waiting")
        self.assertEqual(recovered["sources"][0]["attempts"][0]["status"], "interrupted")
        completed = service.run_pending(job["job_id"])
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["sources"][0]["attempt_count"], 2)

    def test_extractor_failure_is_recorded_per_source_and_other_sources_continue(self):
        batch = _batch(self.store, "Partial", _png((1, 1, 1)), _png((2, 2, 2)))
        calls = 0

        def fail_first(source, mode, config):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("safe synthetic failure")
            return _recipe_rows(source, mode, config)

        service = JobService(self.output, extractor=fail_first)
        job = service.start(batch["batch_id"], "local_ocr")
        snapshot = service.run_pending(job["job_id"])

        self.assertEqual(snapshot["status"], "completed_with_errors")
        self.assertEqual(snapshot["failure_count"], 1)
        self.assertEqual([item["status"] for item in snapshot["sources"]], ["failed", "completed"])
        self.assertEqual(snapshot["sources"][0]["error"], "safe synthetic failure")

    def test_failed_worker_launch_can_resume_existing_job_without_new_upload(self):
        batch = _batch(self.store, "Worker launch retry", _png())
        service = JobService(self.output, extractor=_recipe_rows)
        with patch.object(service, "_launch_worker", side_effect=OSError("synthetic launch failure")):
            failed = service.start(batch["batch_id"], "local_ocr")
        source_id = failed["sources"][0]["source_id"]
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["sources"][0]["status"], "waiting")

        resumed = service.resume(failed["job_id"])
        completed = service.run_pending(failed["job_id"])

        self.assertEqual(resumed["status"], "queued")
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["sources"][0]["source_id"], source_id)
        self.assertEqual(completed["sources"][0]["attempt_count"], 1)

    def test_failed_job_retry_does_not_launch_while_a_worker_holds_job_lease(self):
        batch = _batch(self.store, "Retry lease", _png())
        service = JobService(self.output, extractor=_recipe_rows)
        job = service.start(batch["batch_id"], "local_ocr")
        service._mutate_job(job["job_id"],
                            lambda current: ui_job_service._mark_job_failed(current, "synthetic failure"))

        with _file_lock(service.root / "locks" / f"job-{job['job_id']}.lock"):
            with patch.object(service, "_launch_worker") as launch:
                resumed = service.resume(job["job_id"])

        self.assertEqual(resumed["status"], "failed")
        launch.assert_not_called()

    def test_resume_retries_only_failed_sources_after_partial_completion(self):
        batch = _batch(self.store, "Partial retry", _png((1, 1, 1)), _png((2, 2, 2)))
        calls: list[str] = []
        fail_first_source_once = {"enabled": True}

        def extractor(source, mode, config):
            calls.append(source["source_id"])
            if fail_first_source_once["enabled"]:
                fail_first_source_once["enabled"] = False
                raise RuntimeError("synthetic first attempt failure")
            return _recipe_rows(source, mode, config)

        service = JobService(self.output, extractor=extractor)
        job = service.start(batch["batch_id"], "local_ocr")
        first = service.run_pending(job["job_id"])
        successful_id = next(source["source_id"] for source in first["sources"]
                             if source["status"] == "completed")
        failed_id = next(source["source_id"] for source in first["sources"]
                         if source["status"] == "failed")

        resumed = service.resume(job["job_id"])
        retried = service.run_pending(job["job_id"])

        self.assertEqual(resumed["status"], "queued")
        self.assertEqual(retried["status"], "completed")
        self.assertEqual(retried["sources"][0]["attempt_count"], 2)
        self.assertEqual(retried["sources"][1]["attempt_count"], 1)
        self.assertEqual(calls, [failed_id, successful_id, failed_id])
        self.assertEqual({source["source_id"] for source in retried["sources"]},
                         {failed_id, successful_id})

    def test_fixture_extraction_can_run_on_background_thread(self):
        batch = _batch(self.store, "Fixture background", _png())
        entered = threading.Event()
        release = threading.Event()

        def slow_fixture(source, mode, config):
            entered.set()
            release.wait(timeout=3)
            return _recipe_rows(source, mode, config)

        service = JobService(self.output, extractor=slow_fixture, background=True)
        job = service.start(batch["batch_id"], "local_ocr")
        try:
            self.assertTrue(entered.wait(timeout=2))
            snapshot = service.snapshot(job["job_id"])
            self.assertEqual(snapshot["sources"][0]["status"], "processing")
            service.pause(job["job_id"])
            worker = ui_job_service._BACKGROUND_THREADS[job["job_id"]]
        finally:
            release.set()
        worker.join(timeout=3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(service.snapshot(job["job_id"])["status"], "paused")

    def test_real_worker_start_is_detached_and_fixture_start_does_not_spawn(self):
        batch = _batch(self.store, "Worker", _png())
        with patch.dict(os.environ, {"RECIPE_IMPORTER_FIXTURE_MODE": "0"}), \
             patch("ui_job_service.subprocess.Popen") as popen:
            job = JobService(self.output).start(batch["batch_id"], "local_ocr")

        self.assertEqual(job["status"], "queued")
        popen.assert_called_once()
        self.assertIn("extraction_worker.py", " ".join(str(value) for value in popen.call_args.args[0]))
        self.assertEqual(
            popen.call_args.kwargs["creationflags"],
            subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS,
        )

    def test_real_workers_across_services_share_one_extraction_lease(self):
        batch_a = _batch(self.store, "One", _png((10, 0, 0)))
        batch_b = _batch(self.store, "Two", _png((0, 10, 0)))
        entered = threading.Event()
        waiting = threading.Event()
        release = threading.Event()
        guard = threading.Lock()
        active = 0
        max_active = 0

        def slow(source, mode, config):
            nonlocal active, max_active
            with guard:
                active += 1
                max_active = max(max_active, active)
                entered.set()
            release.wait(timeout=3)
            with guard:
                active -= 1
            return _recipe_rows(source, mode, config)

        first_service = JobService(self.output, extractor=slow)
        second_service = JobService(self.output, extractor=slow)
        first = first_service.start(batch_a["batch_id"], "local_ocr")
        second = second_service.start(batch_b["batch_id"], "local_ocr")
        first_thread = threading.Thread(target=first_service.run_pending, args=(first["job_id"],))
        second_thread = threading.Thread(target=second_service.run_pending, args=(second["job_id"],))
        first_thread.start()
        self.assertTrue(entered.wait(timeout=2))
        second_thread.start()
        original_waiting = ui_job_service._mark_waiting_for_worker

        def signal_waiting(job_state):
            original_waiting(job_state)
            waiting.set()

        with patch("ui_job_service._mark_waiting_for_worker", signal_waiting):
            self.assertTrue(waiting.wait(timeout=2))
        release.set()
        first_thread.join(timeout=4)
        second_thread.join(timeout=4)

        self.assertFalse(first_thread.is_alive())
        self.assertFalse(second_thread.is_alive())
        self.assertEqual(max_active, 1)


if __name__ == "__main__":
    unittest.main()
