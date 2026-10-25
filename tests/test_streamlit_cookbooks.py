import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pymupdf as fitz
from streamlit.testing.v1 import AppTest

from batch_store import BatchStore
from review_store import ReviewStore
from ui_data import load_catalog
from ui_job_service import JobService
import ui_job_service


ROOT = Path(__file__).resolve().parents[1]
UPLOAD_PAGE = ROOT / "app_pages" / "upload.py"
REVIEW_PAGE = ROOT / "app_pages" / "review.py"
HISTORY_PAGE = ROOT / "app_pages" / "import_history.py"


def _write_pdf(path: Path, pages: int = 3) -> None:
    document = fitz.open()
    for page_number in range(1, pages + 1):
        page = document.new_page(width=600, height=800)
        page.insert_text((72, 72), f"Synthetic cookbook page {page_number}")
        page.insert_text((72, 115), "Synthetic soup\n1 cup water\nHeat the water.")
    document.save(path)
    document.close()


class StreamlitCookbookTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.addCleanup(self._join_background_workers)
        self.root = Path(self.temporary.name)
        self.output = self.root / "output"
        self.output.mkdir()
        (self.output / ".fixture-library").write_text("synthetic test library", encoding="utf-8")
        self.source = self.root / "synthetic-cookbook.pdf"
        _write_pdf(self.source)
        self.store = BatchStore(self.output, parent_disk_reserve_bytes=0)
        self.batch = self.store.create("Synthetic cookbook batch")
        self.parent = self.store.stage_parent_pdf(self.batch["batch_id"], self.source)

    @staticmethod
    def _join_background_workers():
        for worker in list(ui_job_service._BACKGROUND_THREADS.values()):
            worker.join(timeout=10)

    def _run_page(self, page: Path, **extra_env):
        values = {
            "RECIPE_IMPORTER_OUTPUT_DIR": str(self.output),
            "RECIPE_IMPORTER_SOURCE_DIR": "",
            "RECIPE_IMPORTER_FIXTURE_MODE": "1",
            **extra_env,
        }
        return patch.dict(os.environ, values, clear=False)

    def test_upload_discovers_parent_pdf_and_explains_manual_selector_refresh(self):
        with self._run_page(UPLOAD_PAGE):
            app = AppTest.from_file(str(UPLOAD_PAGE)).run(timeout=20)
            self.assertFalse(app.exception)
            self.assertTrue(any("run_cookbook_selector.bat" in item.value for item in app.get("markdown")))
            self.assertTrue(any(item.label == "Refresh staged cookbooks" for item in app.button))
            self.assertTrue(any("synthetic-cookbook.pdf" in item.value for item in app.caption))
            self.assertTrue(any("3 pages" in item.value for item in app.caption))

            next(item for item in app.button if item.label == "Refresh staged cookbooks").click().run(timeout=20)
            selector = next(item for item in app.selectbox if item.label == "Staged cookbook PDF")
            self.assertEqual(selector.value, self.parent["document_id"])
            self.assertTrue(any(item.label == "First page" for item in app.number_input))
            self.assertTrue(any(item.label == "Last page" for item in app.number_input))
            self.assertFalse(any("Start selector" in item.label for item in app.button))

    def test_refresh_discovers_new_parent_in_a_selected_existing_batch(self):
        other_batch = self.store.create("Second synthetic batch")
        with self._run_page(UPLOAD_PAGE):
            app = AppTest.from_file(str(UPLOAD_PAGE)).run(timeout=20)
            batch_picker = next(item for item in app.selectbox if item.label == "Select an existing batch")
            app = batch_picker.set_value(other_batch["batch_id"]).run(timeout=20)
            self.assertEqual(next(item for item in app.selectbox if item.label == "Select an existing batch").value,
                             other_batch["batch_id"])

            added_source = self.root / "second-cookbook.pdf"
            _write_pdf(added_source, 4)
            added = self.store.stage_parent_pdf(other_batch["batch_id"], added_source)
            app = next(item for item in app.button if item.label == "Refresh staged cookbooks").click().run(timeout=20)
            picker = next(item for item in app.selectbox if item.label == "Staged cookbook PDF")
            self.assertIn("second-cookbook.pdf", picker.options)
            self.assertTrue(any("second-cookbook.pdf" in item.value for item in app.caption))

    def test_range_commit_is_inclusive_and_removing_pending_selection_keeps_durable_range(self):
        with self._run_page(UPLOAD_PAGE):
            app = AppTest.from_file(str(UPLOAD_PAGE)).run(timeout=20)
            next(item for item in app.number_input if item.label == "First page").set_value(2)
            next(item for item in app.number_input if item.label == "Last page").set_value(3)
            app = next(item for item in app.button if item.label == "Add range to selection").click().run(timeout=20)
            edit_key = f"cookbook_edit_button:{self.batch['batch_id']}:{self.parent['document_id']}:0"
            app = next(item for item in app.button if item.key == edit_key).click().run(timeout=20)
            next(item for item in app.number_input if item.label == "First page").set_value(1)
            next(item for item in app.number_input if item.label == "Last page").set_value(3)
            app = next(item for item in app.button if item.label == "Save range edit").click().run(timeout=20)
            app = next(item for item in app.button if item.label == "Commit selected page ranges").click().run(timeout=20)

            saved = self.store.get(self.batch["batch_id"])["parent_documents"][0]["ranges"]
            self.assertEqual([(item["first_page"], item["last_page"], item["page_count"]) for item in saved], [(1, 3, 3)])
            durable_id = saved[0]["range_id"]
            self.assertTrue(any(durable_id in item.value for item in app.caption))

            next(item for item in app.number_input if item.label == "First page").set_value(2)
            next(item for item in app.number_input if item.label == "Last page").set_value(2)
            app = next(item for item in app.button if item.label == "Add range to selection").click().run(timeout=20)
            app = next(item for item in app.button if item.label == "Remove selected range").click().run(timeout=20)
            self.assertEqual(self.store.get(self.batch["batch_id"])["parent_documents"][0]["ranges"][0]["range_id"], durable_id)

    def test_overlapping_ranges_require_explicit_confirmation(self):
        committed = self.store.create_page_range(self.batch["batch_id"], self.parent["document_id"], 1, 2)
        self.assertNotEqual(committed["status"], "rejected")

        with self._run_page(UPLOAD_PAGE):
            app = AppTest.from_file(str(UPLOAD_PAGE)).run(timeout=20)
            next(item for item in app.number_input if item.label == "First page").set_value(2)
            next(item for item in app.number_input if item.label == "Last page").set_value(3)
            app = next(item for item in app.button if item.label == "Add range to selection").click().run(timeout=20)

            self.assertTrue(any("overlap" in item.value.casefold() for item in app.warning))
            commit = next(item for item in app.button if item.label == "Commit selected page ranges")
            self.assertTrue(commit.disabled)
            self.assertTrue(any("I understand these page ranges overlap" == item.label for item in app.checkbox))

    def test_overlap_acknowledgment_resets_when_pending_range_changes(self):
        self.store.create_page_range(self.batch["batch_id"], self.parent["document_id"], 1, 2)

        with self._run_page(UPLOAD_PAGE):
            app = AppTest.from_file(str(UPLOAD_PAGE)).run(timeout=20)
            next(item for item in app.number_input if item.label == "First page").set_value(2)
            next(item for item in app.number_input if item.label == "Last page").set_value(3)
            app = next(item for item in app.button if item.label == "Add range to selection").click().run(timeout=20)
            checkbox = next(item for item in app.checkbox if item.label == "I understand these page ranges overlap")
            app = checkbox.set_value(True).run(timeout=20)
            self.assertFalse(next(item for item in app.button if item.label == "Commit selected page ranges").disabled)

            app = next(item for item in app.button if item.label.startswith("Edit")).click().run(timeout=20)
            next(item for item in app.number_input if item.label == "First page").set_value(1)
            next(item for item in app.number_input if item.label == "Last page").set_value(3)
            app = next(item for item in app.button if item.label == "Save range edit").click().run(timeout=20)

            self.assertFalse(next(item for item in app.checkbox
                                  if item.label == "I understand these page ranges overlap").value)
            self.assertTrue(next(item for item in app.button if item.label == "Commit selected page ranges").disabled)

    def test_backend_range_rejection_is_shown_and_keeps_selection_for_retry(self):
        with self._run_page(UPLOAD_PAGE):
            app = AppTest.from_file(str(UPLOAD_PAGE)).run(timeout=20)
            next(item for item in app.number_input if item.label == "First page").set_value(2)
            next(item for item in app.number_input if item.label == "Last page").set_value(3)
            app = next(item for item in app.button if item.label == "Add range to selection").click().run(timeout=20)
            with patch.object(BatchStore, "create_page_range",
                              return_value={"status": "rejected", "message": "Synthetic page-range rejection."}):
                app = next(item for item in app.button if item.label == "Commit selected page ranges").click().run(timeout=20)

        self.assertTrue(any("Synthetic page-range rejection" in item.value for item in app.error))
        pending_key = f"cookbook_pending:{self.batch['batch_id']}:{self.parent['document_id']}"
        self.assertEqual(app.session_state[pending_key], [{"first_page": 2, "last_page": 3}])

    def test_page_bound_controls_reject_reverse_ranges(self):
        with self._run_page(UPLOAD_PAGE):
            app = AppTest.from_file(str(UPLOAD_PAGE)).run(timeout=20)
            first = next(item for item in app.number_input if item.label == "First page")
            last = next(item for item in app.number_input if item.label == "Last page")
            self.assertEqual(first.max, 3)
            self.assertEqual(last.max, 3)
            first.set_value(3)
            app = last.set_value(1).run(timeout=20)
            self.assertTrue(any("after the first page" in item.value for item in app.error))
            self.assertTrue(next(item for item in app.button if item.label == "Add range to selection").disabled)

    def test_range_job_requires_cloud_consent_and_completed_candidates_are_import_blocked(self):
        page_range = self.store.create_page_range(self.batch["batch_id"], self.parent["document_id"], 2, 3)
        with self._run_page(UPLOAD_PAGE):
            app = AppTest.from_file(str(UPLOAD_PAGE)).run(timeout=20)
            range_picker = next(item for item in app.multiselect if item.label == "Ranges to extract")
            app = range_picker.set_value([page_range["range_id"]]).run(timeout=20)
            mode = next(item for item in app.segmented_control if item.label == "Cookbook extraction method")
            app = mode.set_value("Gemini extraction").run(timeout=20)
            self.assertTrue(next(item for item in app.button if item.label == "Start selected range extraction").disabled)
            consent = next(item for item in app.checkbox
                           if "simulated and sends no pages to Google" in item.label)
            app = consent.set_value(True).run(timeout=20)
            app = next(item for item in app.button if item.label == "Start selected range extraction").click().run(timeout=20)
            first_job = JobService(self.output).list_range_jobs()[0]
            worker = ui_job_service._BACKGROUND_THREADS.get(first_job["job_id"])
            if worker is not None:
                worker.join(timeout=5)
            app = app.run(timeout=20)
            persisted = JobService(self.output).range_snapshot(first_job["job_id"])
            self.assertTrue(persisted["cloud_consent"])
            self.assertEqual(persisted["status"], "completed")
            self.assertFalse(next(item for item in app.checkbox
                                  if "simulated and sends no pages to Google" in item.label).value)

            start = next(item for item in app.button if item.label == "Start selected range extraction")
            self.assertTrue(start.disabled)
            self.assertEqual(len(JobService(self.output).list_range_jobs()), 1)
            self.assertEqual(JobService(self.output).range_snapshot(first_job["job_id"])["ranges"][0]["attempt_count"], 1)

        service = JobService(self.output, {"extractor_version": "test-range-ui-v1"},
                             extractor=lambda *_: [
                                 {"name": "Synthetic soup", "ingredients": ["1 cup water"],
                                  "instructions": ["Heat the water."]},
                                 {"name": "Synthetic tea", "ingredients": ["1 cup water"],
                                  "instructions": ["Steep the tea."]},
                             ])
        job = service.start_range_job(self.batch["batch_id"], [page_range["range_id"]], "local_ocr")
        completed = service.run_range_pending(job["job_id"])
        self.assertEqual(completed["status"], "completed")
        registered = service.register_completed_ranges(job["job_id"])
        self.assertEqual(len(registered), 2)

        views = load_catalog(self.output)
        view = next(item for item in views if item.candidate_id == registered[0]["candidate_id"])
        revision = ReviewStore(self.output / "review_events.jsonl").save_revision(
            candidate_id=view.candidate_id, source_hash=view.source_hash, extraction_id=view.extraction_id,
            expected_parent=0, operation_id="test-range-approved-revision", recipe=view.candidate.model_dump(mode="json"),
            target_bindings=list(view.target_bindings),
        )
        ReviewStore(self.output / "review_events.jsonl").approve_revision(
            candidate_id=view.candidate_id, source_hash=view.source_hash, extraction_id=view.extraction_id,
            revision=revision["revision"], operation_id="test-range-approved", source_path=view.source_path,
        )
        approved_view = next(item for item in load_catalog(self.output)
                             if item.candidate_id == registered[0]["candidate_id"])

        with self._run_page(HISTORY_PAGE):
            history = AppTest.from_file(str(HISTORY_PAGE)).run(timeout=20)

        self.assertTrue(approved_view.range_provenance)
        self.assertFalse(approved_view.import_ready)
        self.assertFalse(any(item.value == "Synthetic soup" for item in history.multiselect))
        self.assertTrue(any("cookbook range recipe(s) are excluded" in item.value for item in history.info))

    def test_cookbook_cloud_consent_is_invalidated_when_selected_ranges_change(self):
        first = self.store.create_page_range(self.batch["batch_id"], self.parent["document_id"], 1, 1)
        second = self.store.create_page_range(self.batch["batch_id"], self.parent["document_id"], 2, 2)
        other_batch = self.store.create("Another synthetic cookbook")
        other_parent = self.store.stage_parent_pdf(other_batch["batch_id"], self.source)
        self.store.create_page_range(other_batch["batch_id"], other_parent["document_id"], 1, 1)

        with self._run_page(UPLOAD_PAGE):
            app = AppTest.from_file(str(UPLOAD_PAGE)).run(timeout=20)
            app = next(item for item in app.selectbox if item.label == "Select an existing batch").set_value(
                self.batch["batch_id"]).run(timeout=20)
            picker = next(item for item in app.multiselect if item.label == "Ranges to extract")
            app = picker.set_value([first["range_id"]]).run(timeout=20)
            app = next(item for item in app.segmented_control
                       if item.label == "Cookbook extraction method").set_value("Gemini extraction").run(timeout=20)
            consent = next(item for item in app.checkbox if "simulated and sends no pages to Google" in item.label)
            app = consent.set_value(True).run(timeout=20)
            self.assertFalse(next(item for item in app.button
                                  if item.label == "Start selected range extraction").disabled)

            app = next(item for item in app.segmented_control
                       if item.label == "Cookbook extraction method").set_value("Local OCR").run(timeout=20)
            app = next(item for item in app.segmented_control
                       if item.label == "Cookbook extraction method").set_value("Gemini extraction").run(timeout=20)
            self.assertFalse(next(item for item in app.checkbox
                                  if "simulated and sends no pages to Google" in item.label).value)

            app = next(item for item in app.checkbox
                       if "simulated and sends no pages to Google" in item.label).set_value(True).run(timeout=20)
            app = next(item for item in app.selectbox
                       if item.label == "Select an existing batch").set_value(other_batch["batch_id"]).run(timeout=20)
            app = next(item for item in app.selectbox
                       if item.label == "Select an existing batch").set_value(self.batch["batch_id"]).run(timeout=20)
            app = next(item for item in app.segmented_control
                       if item.label == "Cookbook extraction method").set_value("Gemini extraction").run(timeout=20)
            self.assertFalse(next(item for item in app.checkbox
                                  if "simulated and sends no pages to Google" in item.label).value)

            app = next(item for item in app.multiselect if item.label == "Ranges to extract").set_value(
                [second["range_id"]]).run(timeout=20)
            consent = next(item for item in app.checkbox if "simulated and sends no pages to Google" in item.label)
            self.assertFalse(consent.value)
            self.assertTrue(next(item for item in app.button
                                 if item.label == "Start selected range extraction").disabled)

    def test_changed_range_extraction_configuration_can_start_new_job(self):
        page_range = self.store.create_page_range(self.batch["batch_id"], self.parent["document_id"], 1, 1)
        extractor = lambda *_: [{"name": "Synthetic configuration result"}]
        first_service = JobService(self.output, {"extractor_version": "fixture-range-v1"}, extractor)
        first = first_service.start_range_job(self.batch["batch_id"], [page_range["range_id"]], "local_ocr")
        first_service.run_range_pending(first["job_id"])

        with self._run_page(UPLOAD_PAGE):
            with patch.object(JobService, "range_config_fingerprint", return_value="2" * 64):
                app = AppTest.from_file(str(UPLOAD_PAGE)).run(timeout=20)
                self.assertFalse(app.exception)
                app = next(item for item in app.multiselect if item.label == "Ranges to extract").set_value(
                    [page_range["range_id"]]).run(timeout=20)
                start = next(item for item in app.button if item.label == "Start selected range extraction")
                self.assertFalse(start.disabled)

    def test_review_opens_at_range_start_and_explains_import_block(self):
        page_range = self.store.create_page_range(self.batch["batch_id"], self.parent["document_id"], 2, 3)
        service = JobService(self.output, {"extractor_version": "test-range-review-v1"},
                             extractor=lambda *_: [{"name": "Synthetic soup", "ingredients": ["1 cup water"],
                                                    "instructions": ["Heat the water."]}])
        job = service.start_range_job(self.batch["batch_id"], [page_range["range_id"]], "local_ocr")
        service.run_range_pending(job["job_id"])
        registration = service.register_completed_ranges(job["job_id"])[0]

        with self._run_page(REVIEW_PAGE):
            app = AppTest.from_file(str(REVIEW_PAGE)).run(timeout=20)
            self.assertFalse(app.exception)
            page = next(item for item in app.selectbox if item.label == "Page")
            self.assertEqual(page.value, 2)
            self.assertTrue(any("synthetic-cookbook.pdf" in item.value for item in app.markdown))
            visible_notices = [item.value for kind in ("info", "warning", "success") for item in app.get(kind)]
            self.assertTrue(any("original pages 2-3" in text.casefold() for text in visible_notices))
            self.assertTrue(any("cannot be imported into Mealie" in text for text in visible_notices))

            next(item for item in app.text_input if item.label == "Recipe title").set_value("Unsaved range title")
            app = next(item for item in app.selectbox if item.label == "Page").set_value(3).run(timeout=20)
            self.assertFalse(app.exception)
            self.assertEqual(next(item for item in app.text_input if item.label == "Recipe title").value,
                             "Unsaved range title")
            self.assertIn(registration["candidate_id"], app.session_state["review_buffers"])
            app = next(item for item in app.button if item.label == "Save draft").click().run(timeout=20)
            self.assertTrue(any("Draft saved (revision 1)" in item.value for item in app.caption))
            app = next(item for item in app.button if item.label == "Approve & next").click().run(timeout=20)

        approved = next(item for item in ReviewStore(self.output / "review_events.jsonl").approvals[
            registration["candidate_id"]
        ])
        approved_view = next(item for item in load_catalog(self.output)
                             if item.candidate_id == registration["candidate_id"])
        self.assertEqual(approved["revision"], 1)
        self.assertFalse(approved_view.import_ready)


if __name__ == "__main__":
    unittest.main()
