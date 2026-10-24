from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path

import fitz
from PIL import Image

from batch_store import BatchStore


def _image_bytes(kind: str, color: tuple[int, int, int] = (30, 80, 140)) -> bytes:
    image = Image.new("RGB", (8, 8), color)
    output = io.BytesIO()
    image.save(output, format=kind)
    return output.getvalue()


def _pdf_bytes(pages: int = 1) -> bytes:
    document = fitz.open()
    for index in range(pages):
        page = document.new_page()
        page.insert_text((72, 72), f"Synthetic recipe page {index + 1}")
    content = document.tobytes()
    document.close()
    return content


class BatchStoreTests(unittest.TestCase):
    def test_default_limits_reject_a_whole_cookbook_pdf_without_staging_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = BatchStore(Path(tmp) / "output")
            batch = store.create("Whole cookbook safety boundary")
            self.assertEqual(store.max_file_bytes, 50 * 1024 * 1024)
            self.assertEqual(store.max_pdf_pages, 100)

            rejected = store.stage(batch["batch_id"], "whole-book.pdf", _pdf_bytes(101))
            reloaded = BatchStore(Path(tmp) / "output").get(batch["batch_id"])

        self.assertEqual(rejected["status"], "rejected")
        self.assertIn("100-page limit", rejected["reason"])
        self.assertEqual(reloaded["sources"], [])

    def test_create_and_stage_supported_formats_with_durable_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "output"
            store = BatchStore(output)
            batch = store.create("  Weeknight scans  ", "  Weeknight  ")

            inputs = [
                ("meal.pdf", _pdf_bytes(2), "pdf", 2),
                ("meal.jpg", _image_bytes("JPEG"), "jpeg", 1),
                ("meal.jpeg", _image_bytes("JPEG", (90, 40, 20)), "jpeg", 1),
                ("meal.png", _image_bytes("PNG"), "png", 1),
                ("meal.webp", _image_bytes("WEBP"), "webp", 1),
            ]
            results = [store.stage(batch["batch_id"], name, data) for name, data, _, _ in inputs]
            reopened = BatchStore(output).get(batch["batch_id"])

            self.assertEqual(batch["name"], "Weeknight scans")
            self.assertEqual(batch["tag"], "Weeknight")
            self.assertEqual([item["status"] for item in results], ["staged"] * 5)
            self.assertEqual([item["format"] for item in results], [item[2] for item in inputs])
            self.assertEqual([item["page_count"] for item in results], [item[3] for item in inputs])
            self.assertEqual(len(reopened["sources"]), 5)
            self.assertTrue(all(Path(item["staged_path"]).is_file() for item in results))
            self.assertTrue(all(Path(item["staged_path"]).resolve().is_relative_to(output.resolve()) for item in results))
            self.assertEqual([item["original_filename"] for item in results], [item[0] for item in inputs])

    def test_same_filename_different_content_gets_distinct_collision_safe_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = BatchStore(Path(tmp) / "output")
            batch = store.create("same names")
            first = store.stage(batch["batch_id"], "dish.png", _image_bytes("PNG", (1, 2, 3)))
            second = store.stage(batch["batch_id"], "dish.png", _image_bytes("PNG", (4, 5, 6)))

            self.assertNotEqual(first["digest"], second["digest"])
            self.assertNotEqual(first["staged_path"], second["staged_path"])
            self.assertEqual(first["original_filename"], second["original_filename"])
            self.assertEqual(len(store.get(batch["batch_id"])["sources"]), 2)

    def test_duplicate_digest_returns_existing_work_without_restaging(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = BatchStore(Path(tmp) / "output")
            first_batch = store.create("First")
            second_batch = store.create("Second")
            content = _image_bytes("PNG")
            first = store.stage(first_batch["batch_id"], "original.png", content)
            duplicate = store.stage(second_batch["batch_id"], "renamed.png", content)

            self.assertEqual(duplicate["status"], "duplicate")
            self.assertEqual(duplicate["digest"], first["digest"])
            self.assertEqual(duplicate["existing_source_id"], first["source_id"])
            self.assertFalse(duplicate["linked_to_batch"])
            self.assertEqual(store.get(second_batch["batch_id"])["sources"], [])

    def test_exact_reupload_restores_missing_staged_source_for_existing_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = BatchStore(Path(tmp) / "output")
            original_batch = store.create("Original")
            recovery_batch = store.create("Recovery")
            content = _image_bytes("PNG")
            original = store.stage(original_batch["batch_id"], "family-scan.png", content)
            Path(original["staged_path"]).unlink()

            restored = store.stage(recovery_batch["batch_id"], "rescanned-name.png", content)

            self.assertEqual(restored["status"], "relinked")
            self.assertEqual(restored["existing_batch_id"], original_batch["batch_id"])
            self.assertEqual(restored["existing_source_id"], original["source_id"])
            self.assertEqual(Path(original["staged_path"]).read_bytes(), content)
            self.assertEqual(store.get(recovery_batch["batch_id"])["sources"], [])

    def test_invalid_magic_unsupported_type_and_extension_mismatch_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = BatchStore(Path(tmp) / "output")
            batch = store.create("Validation")

            unsupported = store.stage(batch["batch_id"], "notes.txt", b"plain text")
            corrupt = store.stage(batch["batch_id"], "bad.png", b"\x89PNG\r\n\x1a\nnot an image")
            mismatch = store.stage(batch["batch_id"], "wrong.jpg", _image_bytes("PNG"))
            corrupt_pdf = store.stage(batch["batch_id"], "bad.pdf", b"%PDF-1.7\nnot a pdf")

            self.assertEqual([item["status"] for item in (unsupported, corrupt, mismatch, corrupt_pdf)], ["rejected"] * 4)
            self.assertTrue(all(item["reason"] for item in (unsupported, corrupt, mismatch, corrupt_pdf)))
            self.assertEqual(store.get(batch["batch_id"])["sources"], [])

    def test_limits_are_enforced_and_partial_success_survives_reload(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "output"
            store = BatchStore(output, max_file_bytes=20_000, max_pdf_pages=2)
            batch = store.create("Bounded")
            accepted = store.stage(batch["batch_id"], "okay.png", _image_bytes("PNG"))
            too_large = store.stage(batch["batch_id"], "large.jpg", _image_bytes("JPEG") + b"x" * 20_001)
            too_many_pages = store.stage(batch["batch_id"], "pages.pdf", _pdf_bytes(3))

            self.assertEqual(accepted["status"], "staged")
            self.assertEqual(too_large["status"], "rejected")
            self.assertIn("size", too_large["reason"].casefold())
            self.assertEqual(too_many_pages["status"], "rejected")
            self.assertIn("page", too_many_pages["reason"].casefold())
            self.assertEqual(len(BatchStore(output).get(batch["batch_id"])["sources"]), 1)

    def test_rejects_path_like_names_without_using_them_as_storage_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "output"
            store = BatchStore(output)
            batch = store.create("Safe path")
            result = store.stage(batch["batch_id"], r"C:\private\..\dish.png", _image_bytes("PNG"))

            self.assertEqual(result["status"], "staged")
            self.assertEqual(Path(result["staged_path"]).resolve().parent, (output / "ui_batches" / batch["batch_id"] / "sources").resolve())
            self.assertNotIn("private", result["staged_path"])


if __name__ == "__main__":
    unittest.main()
