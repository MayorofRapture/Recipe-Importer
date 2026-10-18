from __future__ import annotations

import os
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image
import io

import ui_job_service
from batch_store import BatchStore, _file_lock
from fixture_extraction import create_fixture_extractor
from recipe_quality import ExtractionBatch, RecipeCandidate
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


def _recipe_rows(source: dict, mode: str, config: dict):
    return [{
        "status": "EXTRACTED_ONLY", "title": f"Synthetic {source['original_filename']}",
        "candidate_id": f"candidate-{source['digest'][:8]}", "route_reason": mode,
    }]


class JobServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.output = Path(self.tmp.name) / "output"
        self.store = BatchStore(self.output)
        self.fixture_env = patch.dict(os.environ, {"RECIPE_IMPORTER_FIXTURE_MODE": "1"})
        self.fixture_env.start()
        self.addCleanup(self.fixture_env.stop)
        self.addCleanup(self.tmp.cleanup)

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
