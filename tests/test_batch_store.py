from __future__ import annotations

import io
import hashlib
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

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


def _write_pdf(path: Path, pages: int = 1, *, encrypted: bool = False) -> None:
    document = fitz.open()
    for index in range(pages):
        document.new_page().insert_text((72, 72), f"Synthetic cookbook page {index + 1}")
    options = {"encryption": fitz.PDF_ENCRYPT_AES_256, "owner_pw": "owner", "user_pw": "user"} if encrypted else {}
    document.save(path, **options)
    document.close()


class BatchStoreTests(unittest.TestCase):
    def test_page_range_materializes_only_selected_pages_and_keeps_original_provenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = BatchStore(root / "output", parent_disk_reserve_bytes=0)
            batch = store.create("Page ranges")
            source = root / "book.pdf"
            _write_pdf(source, 105)
            original_hash = hashlib.sha256(source.read_bytes()).hexdigest()
            parent = store.stage_parent_pdf(batch["batch_id"], source)

            result = store.create_page_range(batch["batch_id"], parent["document_id"], 101, 103)
            range_path = Path(result["input_path"])
            with fitz.open(range_path) as document:
                text = [page.get_text().strip() for page in document]

            reopened = store.get(batch["batch_id"])
            record = reopened["parent_documents"][0]["ranges"][0]
            self.assertEqual(result["status"], "staged")
            self.assertEqual(len(text), 3)
            self.assertIn("page 101", text[0])
            self.assertIn("page 102", text[1])
            self.assertIn("page 103", text[2])
            self.assertEqual(record["first_page"], 101)
            self.assertEqual(record["last_page"], 103)
            self.assertEqual(record["page_count"], 3)
            self.assertEqual(record["parent_sha256"], original_hash)
            self.assertEqual(record["input_sha256"], hashlib.sha256(range_path.read_bytes()).hexdigest())
            self.assertEqual(record["status"], "waiting")
            self.assertEqual(reopened["sources"], [])
            self.assertEqual(source.read_bytes(), Path(parent["staged_path"]).read_bytes())

    def test_page_range_identity_is_versioned_and_idempotent_while_overlaps_are_separate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = BatchStore(root / "output", parent_disk_reserve_bytes=0)
            batch = store.create("Overlapping ranges")
            source = root / "book.pdf"
            _write_pdf(source, 4)
            parent = store.stage_parent_pdf(batch["batch_id"], source)

            first = store.create_page_range(batch["batch_id"], parent["document_id"], 1, 3)
            repeated = store.create_page_range(batch["batch_id"], parent["document_id"], 1, 3)
            overlap = store.create_page_range(batch["batch_id"], parent["document_id"], 2, 4)
            expected = hashlib.sha256(b"\0".join((
                b"recipe-importer:page-range:v1", parent["sha256"].encode("ascii"),
                b"1", b"3",
            ))).hexdigest()

            ranges = store.get(batch["batch_id"])["parent_documents"][0]["ranges"]
            self.assertEqual(first["range_id"], expected)
            self.assertEqual(first["record_version"], 1)
            self.assertEqual(repeated["status"], "duplicate")
            self.assertEqual(repeated["input_sha256"], first["input_sha256"])
            self.assertNotEqual(first["range_id"], overlap["range_id"])
            self.assertEqual(len(ranges), 2)

    def test_page_ranges_from_different_parent_hashes_have_distinct_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = BatchStore(root / "output", parent_disk_reserve_bytes=0)
            batch = store.create("Different parents")
            paths = [root / "one.pdf", root / "two.pdf"]
            _write_pdf(paths[0], 1)
            doc = fitz.open()
            doc.new_page().insert_text((72, 72), "Synthetic cookbook page 1")
            doc.set_metadata({"author": "different source"})
            doc.save(paths[1])
            doc.close()
            parents = [store.stage_parent_pdf(batch["batch_id"], path) for path in paths]

            ranges = [store.create_page_range(batch["batch_id"], item["document_id"], 1, 1)
                      for item in parents]

            self.assertNotEqual(parents[0]["sha256"], parents[1]["sha256"])
            self.assertNotEqual(ranges[0]["range_id"], ranges[1]["range_id"])

    def test_page_range_rejects_invalid_bounds_and_configured_limits(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = BatchStore(root / "output", parent_disk_reserve_bytes=0,
                               max_page_range_pages=2, max_page_range_bytes=1024)
            batch = store.create("Bounds")
            source = root / "book.pdf"
            _write_pdf(source, 3)
            parent = store.stage_parent_pdf(batch["batch_id"], source)

            outcomes = [store.create_page_range(batch["batch_id"], parent["document_id"], a, b)
                        for a, b in [(0, 1), (-1, 1), (2, 1), (1, 4), (1, 3)]]

            self.assertTrue(all(item["status"] == "rejected" for item in outcomes))
            self.assertEqual(store.get(batch["batch_id"])["parent_documents"][0].get("ranges", []), [])

    def test_page_range_enforces_generated_file_size_limit_before_publication(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = BatchStore(root / "output", parent_disk_reserve_bytes=0,
                               max_page_range_bytes=1)
            batch = store.create("Range size limit")
            source = root / "book.pdf"
            _write_pdf(source, 1)
            parent = store.stage_parent_pdf(batch["batch_id"], source)

            result = store.create_page_range(batch["batch_id"], parent["document_id"], 1, 1)

            range_dir = Path(parent["staged_path"]).parent / "ranges"
            self.assertEqual(result["status"], "rejected")
            self.assertIn("byte limit", result["reason"].casefold())
            self.assertEqual(store.get(batch["batch_id"])["parent_documents"][0].get("ranges", []), [])
            self.assertEqual(list(range_dir.glob("*.pdf")), [])
            self.assertEqual(list(range_dir.glob(".*.tmp")), [])

    def test_page_range_serializer_checks_size_before_creating_temporary_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = BatchStore(root / "output", parent_disk_reserve_bytes=0)
            batch = store.create("Serializer size limit")
            source = root / "book.pdf"
            _write_pdf(source, 1)
            parent = store.stage_parent_pdf(batch["batch_id"], source)
            temporary_range = root / "range.tmp"

            with fitz.open(parent["staged_path"]) as document:
                with self.assertRaisesRegex(ValueError, "byte limit"):
                    store._write_page_range_pdf(document, temporary_range, 1, 1, max_bytes=1)

            self.assertFalse(temporary_range.exists())

    def test_page_range_blocks_parent_hash_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = BatchStore(root / "output", parent_disk_reserve_bytes=0)
            batch = store.create("Corrupt parent")
            source = root / "book.pdf"
            _write_pdf(source, 2)
            parent = store.stage_parent_pdf(batch["batch_id"], source)
            Path(parent["staged_path"]).write_bytes(b"tampered")

            result = store.create_page_range(batch["batch_id"], parent["document_id"], 1, 1)

            self.assertEqual(result["status"], "rejected")
            self.assertIn("hash", result["reason"].casefold())
            self.assertEqual(store.get(batch["batch_id"])["parent_documents"][0].get("ranges", []), [])

    def test_page_range_manifest_failure_recovers_without_duplicate_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = BatchStore(root / "output", parent_disk_reserve_bytes=0)
            batch = store.create("Metadata recovery")
            source = root / "book.pdf"
            _write_pdf(source, 2)
            parent = store.stage_parent_pdf(batch["batch_id"], source)

            with patch.object(store, "_write_manifest", side_effect=OSError("metadata unavailable")):
                failed = store.create_page_range(batch["batch_id"], parent["document_id"], 1, 2)
            recovered = store.create_page_range(batch["batch_id"], parent["document_id"], 1, 2)

            self.assertEqual(failed["status"], "rejected")
            self.assertEqual(recovered["status"], "recovered")
            self.assertEqual(len(store.get(batch["batch_id"])["parent_documents"][0]["ranges"]), 1)

    def test_page_range_generation_interruption_leaves_no_record_or_temporary_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = BatchStore(root / "output", parent_disk_reserve_bytes=0)
            batch = store.create("Interrupted generation")
            source = root / "book.pdf"
            _write_pdf(source, 2)
            parent = store.stage_parent_pdf(batch["batch_id"], source)

            with patch.object(store, "_write_page_range_pdf", side_effect=OSError("interrupted")):
                result = store.create_page_range(batch["batch_id"], parent["document_id"], 1, 2)

            range_dir = Path(parent["staged_path"]).parent / "ranges"
            self.assertEqual(result["status"], "rejected")
            self.assertEqual(store.get(batch["batch_id"])["parent_documents"][0].get("ranges", []), [])
            self.assertEqual(list(range_dir.glob(".*.tmp")), [])

    def test_page_range_does_not_accept_or_overwrite_missing_or_damaged_input_with_new_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = BatchStore(root / "output", parent_disk_reserve_bytes=0)
            batch = store.create("Range recovery")
            source = root / "book.pdf"
            _write_pdf(source, 2)
            parent = store.stage_parent_pdf(batch["batch_id"], source)
            first = store.create_page_range(batch["batch_id"], parent["document_id"], 1, 2)
            range_path = Path(first["input_path"])
            expected_hash = first["input_sha256"]
            range_path.write_bytes(b"damaged")

            damaged = store.create_page_range(batch["batch_id"], parent["document_id"], 1, 2)

            self.assertEqual(damaged["status"], "rejected")
            self.assertEqual(range_path.read_bytes(), b"damaged")
            range_path.unlink()
            missing = store.create_page_range(batch["batch_id"], parent["document_id"], 1, 2)
            self.assertEqual(missing["status"], "rejected")
            self.assertFalse(range_path.exists())
            self.assertEqual(store.get(batch["batch_id"])["parent_documents"][0]["ranges"][0]["input_sha256"], expected_hash)

    def test_concurrent_identical_page_range_creation_keeps_one_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = BatchStore(root / "output", parent_disk_reserve_bytes=0)
            batch = store.create("Concurrent ranges")
            source = root / "book.pdf"
            _write_pdf(source, 4)
            parent = store.stage_parent_pdf(batch["batch_id"], source)
            results = []
            threads = [threading.Thread(target=lambda: results.append(
                store.create_page_range(batch["batch_id"], parent["document_id"], 2, 4)))
                for _ in range(4)]

            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)

            self.assertTrue(all(not thread.is_alive() for thread in threads))
            self.assertEqual(sorted(item["status"] for item in results),
                             ["duplicate", "duplicate", "duplicate", "staged"])
            self.assertEqual(len(store.get(batch["batch_id"])["parent_documents"][0]["ranges"]), 1)

    def test_page_range_reads_legacy_parent_metadata_and_rejects_corrupt_materialized_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = BatchStore(root / "output", parent_disk_reserve_bytes=0)
            batch = store.create("Legacy parent")
            source = root / "book.pdf"
            _write_pdf(source, 1)
            parent = store.stage_parent_pdf(batch["batch_id"], source)
            legacy = {key: value for key, value in batch.items() if key != "parent_documents"}
            legacy["parent_documents"] = [{key: value for key, value in parent.items()
                                           if key != "ranges"}]
            store._write_manifest({"schema_version": 1,
                                   "batches": {batch["batch_id"]: legacy}})

            result = store.create_page_range(batch["batch_id"], parent["document_id"], 1, 1)

            self.assertEqual(result["status"], "staged")
            self.assertEqual(store.get(batch["batch_id"])["parent_documents"][0]["ranges"][0]["schema_version"], 1)

    def test_parent_pdf_stages_in_separate_versioned_metadata_without_changing_sources(self):
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        root = Path(temporary_directory.name)
        output = root / "output"
        store = BatchStore(output)
        batch = store.create("Cookbook")
        source = root / "family recipes.pdf"
        _write_pdf(source, 2)
        original_bytes = source.read_bytes()

        staged = store.stage_parent_pdf(batch["batch_id"], source)
        reopened = BatchStore(output).get(batch["batch_id"])

        self.assertEqual(staged["status"], "staged")
        self.assertEqual(staged["record_version"], 1)
        self.assertEqual(staged["kind"], "parent_pdf")
        self.assertEqual(staged["sha256"], hashlib.sha256(original_bytes).hexdigest())
        self.assertEqual(staged["document_id"], staged["sha256"])
        self.assertEqual(staged["original_filename"], "family recipes.pdf")
        self.assertEqual(staged["size_bytes"], len(original_bytes))
        self.assertEqual(staged["page_count"], 2)
        self.assertEqual(staged["batch_id"], batch["batch_id"])
        self.assertEqual(Path(staged["staged_path"]).read_bytes(), original_bytes)
        self.assertEqual(source.read_bytes(), original_bytes)
        self.assertTrue(Path(staged["staged_path"]).is_relative_to(output / "ui_batches" / batch["batch_id"] / "parent_documents"))
        self.assertEqual(reopened["sources"], [])
        self.assertEqual(reopened["parent_documents"], [staged])
        self.assertNotIn("source_id", staged)
        self.assertNotIn("digest", staged)

    def test_parent_pdf_supports_files_over_ordinary_50_mib_limit_with_bounded_copy(self):
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        root = Path(temporary_directory.name)
        output = root / "output"
        store = BatchStore(output)
        batch = store.create("Large cookbook")
        source = root / "over-50-mib.pdf"
        _write_pdf(source)
        target_size = 50 * 1024 * 1024 + 1
        with source.open("ab") as handle:
            remaining = target_size - source.stat().st_size
            block = b"%" * (1024 * 1024)
            while remaining:
                chunk = block[:min(len(block), remaining)]
                handle.write(chunk)
                remaining -= len(chunk)
        original_hash = hashlib.sha256()
        with source.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                original_hash.update(chunk)

        staged = store.stage_parent_pdf(batch["batch_id"], source)

        self.assertEqual(staged["status"], "staged")
        self.assertGreater(staged["size_bytes"], 50 * 1024 * 1024)
        self.assertEqual(staged["sha256"], original_hash.hexdigest())
        self.assertEqual(Path(staged["staged_path"]).stat().st_size, target_size)
        self.assertEqual(store.get(batch["batch_id"])["sources"], [])

    def test_parent_pdf_accepts_more_than_100_pages_within_parent_page_limit(self):
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        root = Path(temporary_directory.name)
        store = BatchStore(root / "output")
        batch = store.create("Large page count")
        source = root / "101-pages.pdf"
        _write_pdf(source, 101)

        staged = store.stage_parent_pdf(batch["batch_id"], source)

        self.assertEqual(staged["status"], "staged")
        self.assertEqual(staged["page_count"], 101)

    def test_parent_pdf_duplicate_content_has_one_consistent_document_record(self):
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        root = Path(temporary_directory.name)
        store = BatchStore(root / "output")
        batch = store.create("Duplicate")
        first_path = root / "first.pdf"
        second_path = root / "renamed.pdf"
        _write_pdf(first_path, 3)
        second_path.write_bytes(first_path.read_bytes())

        first = store.stage_parent_pdf(batch["batch_id"], first_path)
        second = store.stage_parent_pdf(batch["batch_id"], second_path)
        records = store.get(batch["batch_id"])["parent_documents"]

        self.assertEqual(first["document_id"], second["document_id"])
        self.assertEqual(second["status"], "duplicate")
        self.assertEqual(second["existing_batch_id"], batch["batch_id"])
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["staged_path"], first["staged_path"])

    def test_parent_pdf_retry_restores_missing_content_without_adding_a_record(self):
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        root = Path(temporary_directory.name)
        store = BatchStore(root / "output", parent_disk_reserve_bytes=0)
        batch = store.create("Restore parent PDF")
        source = root / "book.pdf"
        _write_pdf(source, 2)
        original = source.read_bytes()
        first = store.stage_parent_pdf(batch["batch_id"], source)
        Path(first["staged_path"]).unlink()

        restored = store.stage_parent_pdf(batch["batch_id"], source)
        records = store.get(batch["batch_id"])["parent_documents"]

        self.assertEqual(restored["status"], "relinked")
        self.assertTrue(restored["accepted"])
        self.assertTrue(restored["duplicate"])
        self.assertEqual(restored["document_id"], first["document_id"])
        self.assertEqual(Path(first["staged_path"]).read_bytes(), original)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["status"], "staged")

    def test_parent_pdf_duplicate_across_batches_does_not_create_another_record(self):
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        root = Path(temporary_directory.name)
        store = BatchStore(root / "output")
        first_batch = store.create("First")
        second_batch = store.create("Second")
        first_path = root / "first.pdf"
        second_path = root / "second.pdf"
        _write_pdf(first_path)
        second_path.write_bytes(first_path.read_bytes())

        first = store.stage_parent_pdf(first_batch["batch_id"], first_path)
        duplicate = store.stage_parent_pdf(second_batch["batch_id"], second_path)

        self.assertEqual(duplicate["status"], "duplicate")
        self.assertEqual(duplicate["document_id"], first["document_id"])
        self.assertEqual(duplicate["existing_batch_id"], first_batch["batch_id"])
        self.assertEqual(store.get(second_batch["batch_id"]).get("parent_documents", []), [])

    def test_parent_pdf_rejects_empty_unsupported_corrupt_encrypted_and_oversize_inputs(self):
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        root = Path(temporary_directory.name)
        store = BatchStore(root / "output", max_parent_file_bytes=1024, parent_disk_reserve_bytes=0)
        batch = store.create("Reject invalid")
        empty = root / "empty.pdf"
        empty.write_bytes(b"")
        unsupported = root / "notes.txt"
        unsupported.write_text("not a pdf", encoding="utf-8")
        corrupt = root / "corrupt.pdf"
        corrupt.write_bytes(b"%PDF-1.7\nnot a PDF")
        encrypted = root / "encrypted.pdf"
        _write_pdf(encrypted, encrypted=True)
        oversized = root / "oversized.pdf"
        _write_pdf(oversized)
        with oversized.open("ab") as handle:
            handle.write(b"%" * 2048)

        results = [store.stage_parent_pdf(batch["batch_id"], path) for path in
                   (empty, unsupported, corrupt, encrypted, oversized)]

        self.assertEqual([item["status"] for item in results], ["rejected"] * 5)
        self.assertTrue(all(item["reason"] for item in results))
        self.assertEqual(store.get(batch["batch_id"]).get("parent_documents", []), [])

    def test_parent_pdf_enforces_configurable_page_limit(self):
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        root = Path(temporary_directory.name)
        store = BatchStore(root / "output", max_parent_pdf_pages=2, parent_disk_reserve_bytes=0)
        batch = store.create("Too many pages")
        source = root / "three-pages.pdf"
        _write_pdf(source, 3)

        result = store.stage_parent_pdf(batch["batch_id"], source)

        self.assertEqual(result["status"], "rejected")
        self.assertIn("page", result["reason"].casefold())
        self.assertEqual(store.get(batch["batch_id"]).get("parent_documents", []), [])

    def test_parent_pdf_rejects_insufficient_space_before_copying(self):
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        root = Path(temporary_directory.name)
        store = BatchStore(root / "output", parent_disk_reserve_bytes=128)
        batch = store.create("Low disk")
        source = root / "book.pdf"
        _write_pdf(source)

        with patch("batch_store.shutil.disk_usage", return_value=type("Usage", (), {"free": 1})()):
            result = store.stage_parent_pdf(batch["batch_id"], source)

        self.assertEqual(result["status"], "rejected")
        self.assertIn("space", result["reason"].casefold())
        self.assertEqual(store.get(batch["batch_id"]).get("parent_documents", []), [])
        self.assertFalse(list((root / "output" / "ui_batches").rglob("*.tmp")))

    def test_parent_pdf_stops_and_cleans_up_if_disk_reserve_is_lost_mid_copy(self):
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        root = Path(temporary_directory.name)
        store = BatchStore(root / "output", parent_disk_reserve_bytes=128)
        batch = store.create("Disk fills during copy")
        source = root / "large.pdf"
        _write_pdf(source)
        with source.open("ab") as handle:
            handle.write(b"%" * (17 * 1024 * 1024))
        usage = [type("Usage", (), {"free": source.stat().st_size + 128})(),
                 type("Usage", (), {"free": 1})()]

        with patch("batch_store.shutil.disk_usage", side_effect=usage):
            result = store.stage_parent_pdf(batch["batch_id"], source)

        self.assertEqual(result["status"], "rejected")
        self.assertIn("space", result["reason"].casefold())
        self.assertEqual(store.get(batch["batch_id"]).get("parent_documents", []), [])
        self.assertFalse(list((root / "output" / "ui_batches").rglob("*.tmp")))

    def test_parent_pdf_rejects_source_changed_after_preflight(self):
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        root = Path(temporary_directory.name)
        store = BatchStore(root / "output", parent_disk_reserve_bytes=0)
        batch = store.create("Source changes")
        source = root / "changing.pdf"
        _write_pdf(source)
        original_copy = store._copy_parent_pdf

        def change_then_copy(source_path: Path, temporary_path: Path, expected_size: int,
                             expected_identity: tuple[int, int, int, int]) -> tuple[str, int]:
            with source_path.open("ab") as handle:
                handle.write(b"%changed")
            return original_copy(source_path, temporary_path, expected_size, expected_identity)

        with patch.object(store, "_copy_parent_pdf", side_effect=change_then_copy):
            result = store.stage_parent_pdf(batch["batch_id"], source)

        self.assertEqual(result["status"], "rejected")
        self.assertIn("changed", result["reason"].casefold())
        self.assertEqual(store.get(batch["batch_id"]).get("parent_documents", []), [])
        self.assertFalse(list((root / "output" / "ui_batches").rglob("*.tmp")))

    def test_parent_pdf_cleans_partial_copy_and_does_not_publish_record(self):
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        root = Path(temporary_directory.name)
        store = BatchStore(root / "output", parent_disk_reserve_bytes=0)
        batch = store.create("Interrupted copy")
        source = root / "book.pdf"
        _write_pdf(source)

        def interrupted_copy(source_path: Path, temporary_path: Path, expected_size: int,
                             expected_identity: tuple[int, int, int, int]) -> None:
            temporary_path.write_bytes(b"partial")
            raise OSError("interrupted")

        with patch.object(store, "_copy_parent_pdf", side_effect=interrupted_copy):
            result = store.stage_parent_pdf(batch["batch_id"], source)

        self.assertEqual(result["status"], "rejected")
        self.assertEqual(store.get(batch["batch_id"]).get("parent_documents", []), [])
        self.assertFalse(list((root / "output" / "ui_batches").rglob("*.tmp")))

    def test_parent_pdf_manifest_failure_leaves_valid_file_recoverable_by_retry(self):
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        root = Path(temporary_directory.name)
        store = BatchStore(root / "output", parent_disk_reserve_bytes=0)
        batch = store.create("Metadata retry")
        source = root / "book.pdf"
        _write_pdf(source)

        with patch.object(store, "_write_manifest", side_effect=OSError("manifest unavailable")):
            failed = store.stage_parent_pdf(batch["batch_id"], source)
        recovered = store.stage_parent_pdf(batch["batch_id"], source)
        duplicate = store.stage_parent_pdf(batch["batch_id"], source)

        self.assertEqual(failed["status"], "rejected")
        self.assertIn("metadata", failed["reason"].casefold())
        self.assertTrue(Path(recovered["staged_path"]).is_file())
        self.assertEqual(recovered["status"], "staged")
        self.assertEqual(duplicate["status"], "duplicate")
        self.assertEqual(len(store.get(batch["batch_id"])["parent_documents"]), 1)

    def test_parent_pdf_reads_historical_manifest_without_parent_documents(self):
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        root = Path(temporary_directory.name)
        output = root / "output"
        store = BatchStore(output)
        batch = store.create("Legacy shape")
        legacy = {"schema_version": 1, "batches": {batch["batch_id"]: {
            key: value for key, value in batch.items() if key != "parent_documents"
        }}}
        store.manifest_path.write_text(json.dumps(legacy), encoding="utf-8")
        source = root / "book.pdf"
        _write_pdf(source)

        before = store.get(batch["batch_id"])
        staged = store.stage_parent_pdf(batch["batch_id"], source)
        after = store.get(batch["batch_id"])

        self.assertNotIn("parent_documents", before)
        self.assertEqual(before["sources"], after["sources"])
        self.assertEqual(staged["status"], "staged")
        self.assertEqual(after["parent_documents"], [staged])

    def test_concurrent_parent_pdf_staging_keeps_one_manifest_record(self):
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        root = Path(temporary_directory.name)
        store = BatchStore(root / "output", parent_disk_reserve_bytes=0)
        batch = store.create("Concurrent")
        paths = [root / "one.pdf", root / "two.pdf"]
        _write_pdf(paths[0], 4)
        paths[1].write_bytes(paths[0].read_bytes())
        results = []
        threads = [threading.Thread(target=lambda path=path: results.append(
            store.stage_parent_pdf(batch["batch_id"], path))) for path in paths]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)

        records = store.get(batch["batch_id"])["parent_documents"]

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(sorted(result["status"] for result in results), ["duplicate", "staged"])
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["document_id"], results[0]["document_id"])

    def test_default_limits_reject_a_whole_cookbook_pdf_without_staging_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = BatchStore(Path(tmp) / "output")
            batch = store.create("Whole cookbook safety boundary")
            self.assertEqual(store.max_file_bytes, 50 * 1024 * 1024)
            self.assertEqual(store.max_pdf_pages, 100)
            self.assertEqual(store.max_parent_file_bytes, 256 * 1024 * 1024)
            self.assertEqual(store.max_parent_pdf_pages, 2_000)
            self.assertEqual(store.parent_disk_reserve_bytes, 64 * 1024 * 1024)

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
