import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import fitz

from batch_store import BatchStore
import tools.cookbook_selector as selector


def _write_pdf(path: Path, pages: int = 1) -> None:
    document = fitz.open()
    for number in range(pages):
        document.new_page().insert_text((72, 72), f"Synthetic page {number + 1}")
    document.save(path)
    document.close()


class CookbookSelectorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = BatchStore(self.root / "output", parent_disk_reserve_bytes=0)
        self.batch = self.store.create("Synthetic cookbook batch")

    def test_dialog_cancellation_does_not_change_batch_metadata(self):
        result = selector.choose_and_stage(self.store, self.batch["batch_id"], lambda: "")

        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(self.store.get(self.batch["batch_id"]).get("parent_documents", []), [])

    def test_existing_batches_are_selectable_by_stable_id(self):
        choices = selector.batch_choices(self.store)

        self.assertEqual(len(choices), 1)
        self.assertEqual(choices[0][1], self.batch["batch_id"])
        self.assertIn("Synthetic cookbook batch", choices[0][0])

    def test_selected_pdf_is_staged_and_visible_after_reopening_batch_store(self):
        source = self.root / "synthetic-cookbook.pdf"
        _write_pdf(source, 3)

        result = selector.stage_selected_pdf(self.store, self.batch["batch_id"], source)
        reopened = BatchStore(self.store.output_dir)
        discovered = next(item for item in reopened.list_batches()
                          if item["batch_id"] == self.batch["batch_id"])

        self.assertEqual(result["status"], "staged")
        self.assertEqual(result["page_count"], 3)
        self.assertEqual(len(discovered["parent_documents"]), 1)
        self.assertEqual(discovered["sources"], [])
        self.assertEqual(Path(discovered["parent_documents"][0]["staged_path"]).read_bytes(),
                         source.read_bytes())

    def test_duplicate_parent_pdf_keeps_one_existing_batch_record(self):
        other_batch = self.store.create("Other synthetic batch")
        first_path = self.root / "first.pdf"
        second_path = self.root / "second.pdf"
        _write_pdf(first_path)
        second_path.write_bytes(first_path.read_bytes())
        selector.stage_selected_pdf(self.store, self.batch["batch_id"], first_path)

        result = selector.stage_selected_pdf(self.store, other_batch["batch_id"], second_path)

        self.assertEqual(result["status"], "duplicate")
        self.assertEqual(self.store.get(other_batch["batch_id"]).get("parent_documents", []), [])

    def test_invalid_pdf_is_rejected_without_parent_publication(self):
        source = self.root / "corrupt.pdf"
        source.write_bytes(b"not a PDF")

        result = selector.stage_selected_pdf(self.store, self.batch["batch_id"], source)

        self.assertEqual(result["status"], "rejected")
        self.assertEqual(self.store.get(self.batch["batch_id"]).get("parent_documents", []), [])

    def test_low_disk_rejection_can_be_retried_without_duplicate_record(self):
        source = self.root / "retry.pdf"
        _write_pdf(source)
        actual_usage = shutil.disk_usage(self.root)
        low_usage = type("DiskUsage", (), {"free": 1})()

        with patch("batch_store.shutil.disk_usage", side_effect=[low_usage, actual_usage]):
            failed = selector.stage_selected_pdf(self.store, self.batch["batch_id"], source)
            retried = selector.stage_selected_pdf(self.store, self.batch["batch_id"], source)

        self.assertEqual(failed["status"], "rejected")
        self.assertIn("disk", failed["reason"].casefold())
        self.assertEqual(retried["status"], "staged")
        self.assertEqual(len(self.store.get(self.batch["batch_id"])["parent_documents"]), 1)

    def test_file_symlink_is_rejected_before_parent_staging(self):
        source = self.root / "real.pdf"
        link = self.root / "linked.pdf"
        _write_pdf(source)
        try:
            link.symlink_to(source)
        except OSError as exc:
            self.skipTest(f"file symlinks are unavailable: {type(exc).__name__}")

        result = selector.stage_selected_pdf(self.store, self.batch["batch_id"], link)

        self.assertEqual(result["status"], "rejected")
        self.assertEqual(self.store.get(self.batch["batch_id"]).get("parent_documents", []), [])

    def test_symlink_detection_rejects_before_opening_the_selected_file(self):
        source = self.root / "linked.pdf"
        with patch.object(Path, "is_symlink", autospec=True,
                          side_effect=lambda path: path == source):
            result = selector.stage_selected_pdf(self.store, self.batch["batch_id"], source)

        self.assertEqual(result["status"], "rejected")
        self.assertEqual(self.store.get(self.batch["batch_id"]).get("parent_documents", []), [])

    @unittest.skipUnless(__import__("os").name == "nt", "Windows junction check")
    def test_junction_ancestor_is_rejected_before_parent_staging(self):
        source_dir = self.root / "actual"
        linked_dir = self.root / "linked"
        source_dir.mkdir()
        source = source_dir / "book.pdf"
        _write_pdf(source)
        completed = subprocess.run(
            ["cmd.exe", "/c", "mklink", "/J", str(linked_dir), str(source_dir)],
            capture_output=True, text=True, check=False,
        )
        if completed.returncode:
            self.skipTest(f"directory junction creation is unavailable: {completed.stderr or completed.stdout}")

        result = selector.stage_selected_pdf(self.store, self.batch["batch_id"], linked_dir / "book.pdf")

        self.assertEqual(result["status"], "rejected")
        self.assertEqual(self.store.get(self.batch["batch_id"]).get("parent_documents", []), [])


if __name__ == "__main__":
    unittest.main()
