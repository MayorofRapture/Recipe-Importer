from __future__ import annotations

import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image
from streamlit.testing.v1 import AppTest

import ui_job_service
from batch_store import BatchStore
from ui_data import load_catalog
from ui_job_service import JobService


PAGE = Path(__file__).resolve().parents[1] / "app_pages" / "upload.py"


def _png() -> bytes:
    image = Image.new("RGB", (24, 24), (40, 90, 120))
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


class StreamlitUploadTests(unittest.TestCase):
    def test_new_user_sees_batch_creation_as_the_starting_action(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "output"
            output.mkdir()
            (output / ".fixture-library").write_text("synthetic test library", encoding="utf-8")
            with patch.dict(os.environ, {
                "RECIPE_IMPORTER_OUTPUT_DIR": str(output),
                "RECIPE_IMPORTER_SOURCE_DIR": "",
                "RECIPE_IMPORTER_FIXTURE_MODE": "1",
            }):
                app = AppTest.from_file(str(PAGE)).run(timeout=15)

        self.assertFalse(app.exception)
        self.assertTrue(any(item.label == "Create batch" for item in app.form_submit_button))
        self.assertFalse(app.selectbox)

    def test_cloud_consent_is_cleared_when_batch_source_scope_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "output"
            output.mkdir()
            (output / ".fixture-library").write_text("synthetic test library", encoding="utf-8")
            with patch.dict(os.environ, {
                "RECIPE_IMPORTER_OUTPUT_DIR": str(output),
                "RECIPE_IMPORTER_SOURCE_DIR": "",
                "RECIPE_IMPORTER_FIXTURE_MODE": "1",
            }):
                app = AppTest.from_file(str(PAGE)).run(timeout=15)
                next(item for item in app.text_input if item.label == "Batch name").set_value("Consent scope")
                next(item for item in app.form_submit_button if item.label == "Create batch").click().run(timeout=15)
                uploader = next(item for item in app.file_uploader
                                if item.label == "Add PDF, JPG, PNG, or WEBP scans")
                first = ("one.png", _png(), "image/png")
                second = ("two.png", Image.new("RGB", (25, 25), (90, 40, 120)), "image/png")
                second_image = io.BytesIO()
                second[1].save(second_image, format="PNG")
                second = (second[0], second_image.getvalue(), second[2])
                uploader.set_value([first]).run(timeout=15)
                next(item for item in app.segmented_control
                     if item.label == "Choose an extraction method").set_value("Gemini extraction").run(timeout=15)
                consent = next(item for item in app.checkbox if "simulated" in item.label.lower())
                consent.check().run(timeout=15)
                uploader = next(item for item in app.file_uploader
                                if item.label == "Add PDF, JPG, PNG, or WEBP scans")
                uploader.set_value([first, second]).run(timeout=15)
                consent = next(item for item in app.checkbox if "simulated" in item.label.lower())

        self.assertFalse(app.exception)
        self.assertFalse(consent.value)

    def test_failed_worker_job_exposes_retry_and_preserved_unfinished_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "output"
            output.mkdir()
            (output / ".fixture-library").write_text("synthetic test library", encoding="utf-8")
            with patch.dict(os.environ, {
                "RECIPE_IMPORTER_OUTPUT_DIR": str(output),
                "RECIPE_IMPORTER_SOURCE_DIR": "",
                "RECIPE_IMPORTER_FIXTURE_MODE": "1",
            }):
                batch = BatchStore(output).create("Retry failed job")
                BatchStore(output).stage(batch["batch_id"], "retry.png", _png())
                service = JobService(output, extractor=lambda source, mode, config: [])
                job = service.start(batch["batch_id"], "local_ocr")
                service._mutate_job(job["job_id"],
                                    lambda current: ui_job_service._mark_job_failed(current, "synthetic worker launch failure"))

                app = AppTest.from_file(str(PAGE)).run(timeout=15)

            self.assertFalse(app.exception)
            self.assertTrue(any(item.label == "Resume unfinished files" for item in app.button))
            self.assertTrue(any("1 files unfinished" in item.value for item in app.caption))
            self.assertTrue(any("Completed file results and extraction history are preserved" in item.value
                                for item in app.info))
            self.assertTrue(any("synthetic worker launch failure" in item.value for item in app.warning))

    def test_reupload_restores_missing_source_and_opens_existing_batch(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "output"
            output.mkdir()
            (output / ".fixture-library").write_text("synthetic test library", encoding="utf-8")
            content = _png()
            with patch.dict(os.environ, {
                "RECIPE_IMPORTER_OUTPUT_DIR": str(output),
                "RECIPE_IMPORTER_SOURCE_DIR": "",
                "RECIPE_IMPORTER_FIXTURE_MODE": "1",
            }):
                app = AppTest.from_file(str(PAGE)).run(timeout=15)
                next(item for item in app.text_input if item.label == "Batch name").set_value("Original")
                next(item for item in app.form_submit_button if item.label == "Create batch").click().run(timeout=15)
                next(item for item in app.file_uploader if item.label == "Add PDF, JPG, PNG, or WEBP scans").set_value(
                    [("family-scan.png", content, "image/png")]).run(timeout=15)
                store = BatchStore(output)
                original = store.list_batches()[0]
                staged_path = Path(original["sources"][0]["staged_path"])
                staged_path.unlink()

                next(item for item in app.file_uploader if item.label == "Add PDF, JPG, PNG, or WEBP scans").set_value(
                    [("recovered-scan.png", content, "image/png")]).run(timeout=15)
                relinked_notice = any("Restored recovered-scan.png" in item.value for item in app.success)
                next(item for item in app.button if item.label == "Open existing work").click().run(timeout=15)

            self.assertFalse(app.exception)
            self.assertEqual(Path(original["sources"][0]["staged_path"]).read_bytes(), content)
            selector = next(item for item in app.selectbox if item.label == "Select an existing batch")
            self.assertEqual(selector.value, original["batch_id"])
            self.assertTrue(relinked_notice)

    def test_partial_intake_recovers_and_fixture_job_publishes_review_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "output"
            output.mkdir()
            (output / ".fixture-library").write_text("synthetic test library", encoding="utf-8")
            with patch.dict(os.environ, {
                "RECIPE_IMPORTER_OUTPUT_DIR": str(output),
                "RECIPE_IMPORTER_SOURCE_DIR": "",
                "RECIPE_IMPORTER_FIXTURE_MODE": "1",
            }):
                app = AppTest.from_file(str(PAGE)).run(timeout=15)
                self.assertFalse(app.exception)
                next(item for item in app.text_input if item.label == "Batch name").set_value("Synthetic upload")
                next(item for item in app.form_submit_button if item.label == "Create batch").click().run(timeout=15)

                uploader = next(item for item in app.file_uploader if item.label == "Add PDF, JPG, PNG, or WEBP scans")
                uploader.set_value([
                    ("synthetic.png", _png(), "image/png"),
                    ("broken.pdf", b"not a pdf", "application/pdf"),
                ]).run(timeout=15)

                self.assertFalse(app.exception)
                self.assertTrue(any("Saved synthetic.png" in item.value for item in app.success))
                self.assertTrue(any("Could not add broken.pdf" in item.value for item in app.error))
                batch = BatchStore(output).list_batches()[0]
                self.assertEqual([source["original_filename"] for source in batch["sources"]], ["synthetic.png"])

                next(button for button in app.button if button.label == "Start extraction").click().run(timeout=15)
                self.assertFalse(app.exception)
                service = JobService(output, extractor=lambda source, mode, config: [])
                job = service.list_jobs()[0]
                worker = ui_job_service._BACKGROUND_THREADS.get(job["job_id"])
                if worker is not None:
                    worker.join(timeout=4)
                app.run(timeout=15)

                completed = service.snapshot(job["job_id"])
                self.assertEqual(completed["status"], "completed")
                views = load_catalog(output)
                self.assertTrue(any(view.candidate.name == "Synthetic demo from synthetic" for view in views))
                self.assertTrue(any("No scans are sent to Gemini" in item.value for item in app.info))


if __name__ == "__main__":
    unittest.main()
