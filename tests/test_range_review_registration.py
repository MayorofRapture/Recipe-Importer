import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pymupdf as fitz

from batch_store import BatchStore
from ui_data import load_catalog, range_relative_to_original_page
from review_store import ReviewStore, range_candidate_id
from ui_import_service import ImportService
from ui_job_service import JobService


def _write_pdf(path: Path, pages: list[str]) -> None:
    document = fitz.open()
    for text in pages:
        page = document.new_page()
        page.insert_text((72, 72), text)
    document.save(path)
    document.close()


class RangeReviewRegistrationTests(unittest.TestCase):
    def _completed_range(self, output: Path, parent: Path, first: int, last: int,
                         recipes: list[dict], *, version: str = "extract-v1",
                         register: bool = True):
        store = BatchStore(output)
        batch = store.create("Synthetic cookbook")
        parent_record = store.stage_parent_pdf(batch["batch_id"], parent)
        page_range = store.create_page_range(batch["batch_id"], parent_record["document_id"], first, last)
        service = JobService(output, {"extractor_version": version},
                             extractor=lambda *_: recipes)
        job = service.start_range_job(batch["batch_id"], [page_range["range_id"]], "local_ocr")
        if register:
            completed = service.run_range_pending(job["job_id"])
        else:
            with patch.object(JobService, "register_completed_ranges", return_value=[]):
                completed = service.run_range_pending(job["job_id"])
        self.assertEqual(completed["status"], "completed")
        return service, batch, parent_record, page_range, job["job_id"]

    def test_completed_range_job_registers_candidates_without_catalog_side_effects(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "output"
            source = root / "cookbook.pdf"
            _write_pdf(source, ["recipe"])
            recipe = {"name": "Toast", "ingredients": ["bread"], "instructions": ["toast"]}
            service, batch, parent, page_range, job_id = self._completed_range(
                output, source, 1, 1, [recipe],
            )

            catalog = load_catalog(output)
            events_before = len(ReviewStore(output / "review_events.jsonl").events)
            load_catalog(output)
            events_after = len(ReviewStore(output / "review_events.jsonl").events)

            self.assertEqual(len(catalog), 1)
            self.assertEqual(catalog[0].range_provenance["range_id"], page_range["range_id"])
            self.assertEqual(service.register_completed_ranges(job_id)[0]["candidate_id"],
                             catalog[0].candidate_id)
            self.assertEqual(events_before, events_after)

    def test_completed_range_registers_distinct_occurrences_and_survives_reload(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "output"
            source = root / "cookbook.pdf"
            _write_pdf(source, ["same source page"])
            recipe = {"name": "Toast", "ingredients": ["bread"], "instructions": ["toast"]}
            service, batch, parent, page_range, job_id = self._completed_range(
                output, source, 1, 1, [recipe, dict(recipe)],
            )

            first = service.register_completed_range(batch["batch_id"], parent["document_id"],
                                                     page_range["range_id"], job_id=job_id)
            repeated = service.register_completed_range(batch["batch_id"], parent["document_id"],
                                                        page_range["range_id"], job_id=job_id)
            catalog = load_catalog(output)
            load_catalog(output)
            durable_registrations = [
                item for values in ReviewStore(output / "review_events.jsonl").registrations.values()
                for item in values if item.get("event") == "candidate_registration"
            ]

            self.assertEqual(len(first), 2)
            self.assertEqual(len({item["candidate_id"] for item in first}), 2)
            self.assertEqual([item["candidate_id"] for item in repeated],
                             [item["candidate_id"] for item in first])
            self.assertEqual(len(catalog), 2)
            self.assertEqual({view.source_hash for view in catalog}, {parent["sha256"]})
            self.assertEqual({view.range_provenance["range_id"] for view in catalog},
                             {page_range["range_id"]})
            self.assertEqual(catalog[0].range_provenance["extraction_method"], "local_ocr")
            self.assertEqual(catalog[0].range_provenance["extractor_version"], "extract-v1")
            self.assertEqual(range_relative_to_original_page(catalog[0], 1), 1)
            self.assertEqual(len(durable_registrations), 2)

    def test_equal_recipes_in_distinct_ranges_never_share_review_or_import_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "output"
            source = root / "cookbook.pdf"
            _write_pdf(source, ["same recipe", "same recipe"])
            recipe = {"name": "Soup", "ingredients": ["water"], "instructions": ["heat"]}
            store = BatchStore(output)
            batch = store.create("Overlapping cookbook")
            parent = store.stage_parent_pdf(batch["batch_id"], source)
            jobs = []
            for page in (1, 2):
                page_range = store.create_page_range(batch["batch_id"], parent["document_id"], page, page)
                service = JobService(output, {"extractor_version": "extract-v1"},
                                     extractor=lambda *_: [recipe])
                job = service.start_range_job(batch["batch_id"], [page_range["range_id"]], "local_ocr")
                service.run_range_pending(job["job_id"])
                jobs.append((service, job["job_id"], page_range))

            registered = [service.register_completed_range(
                batch["batch_id"], parent["document_id"], page_range["range_id"], job_id=job_id,
            )[0] for service, job_id, page_range in jobs]

            self.assertNotEqual(registered[0]["candidate_id"], registered[1]["candidate_id"])
            self.assertEqual(registered[0]["source_hash"], parent["sha256"])
            self.assertEqual(registered[1]["source_hash"], parent["sha256"])
            self.assertNotEqual(registered[0]["range_provenance"]["range_id"],
                                registered[1]["range_provenance"]["range_id"])

    def test_identical_titles_with_different_content_register_independently(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "output"
            source = root / "cookbook.pdf"
            _write_pdf(source, ["two recipes"])
            recipes = [
                {"name": "Pancakes", "ingredients": ["flour"], "instructions": ["mix"]},
                {"name": "Pancakes", "ingredients": ["oats"], "instructions": ["stir"]},
            ]
            service, batch, parent, page_range, job_id = self._completed_range(
                output, source, 1, 1, recipes,
            )

            registered = service.register_completed_range(
                batch["batch_id"], parent["document_id"], page_range["range_id"], job_id=job_id,
            )
            catalog = load_catalog(output)

            self.assertEqual(len({item["candidate_id"] for item in registered}), 2)
            self.assertEqual({view.candidate.name for view in catalog}, {"Pancakes"})
            self.assertEqual(len(catalog), 2)

    def test_range_candidates_are_blocked_before_mealie_dispatch(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "output"
            source = root / "cookbook.pdf"
            _write_pdf(source, ["recipe"])
            recipe = {"name": "Tea", "ingredients": ["water"], "instructions": ["boil"]}
            service, batch, parent, page_range, job_id = self._completed_range(output, source, 1, 1, [recipe])
            registration = service.register_completed_range(
                batch["batch_id"], parent["document_id"], page_range["range_id"], job_id=job_id,
            )[0]
            calls = []
            imported = ImportService(output, client=object())

            result = imported.dispatch_cli_candidate(
                registration["candidate_id"], recipe, str(source), parent["sha256"],
                registration["extraction_id"], lambda *_: calls.append("create"),
            )
            preview = imported.preview([registration["candidate_id"]])

            self.assertEqual(result["status"], "not_sent")
            self.assertEqual(calls, [])
            self.assertEqual(preview["eligible"], [])
            self.assertIn("blocked", preview["excluded"][0]["reason"])

    def test_multpage_range_keeps_truthful_parent_provenance_and_page_mapping(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "output"
            source = root / "grandmas-book.pdf"
            _write_pdf(source, ["cover", "recipe begins", "recipe ends", "index"])
            recipe = {"name": "Bread", "ingredients": ["flour"], "instructions": ["bake"]}
            service, batch, parent, page_range, job_id = self._completed_range(
                output, source, 2, 3, [recipe],
            )

            registration = service.register_completed_range(
                batch["batch_id"], parent["document_id"], page_range["range_id"], job_id=job_id,
            )[0]
            view = next(view for view in load_catalog(output)
                        if view.candidate_id == registration["candidate_id"])

            self.assertEqual(registration["source_hash"], parent["sha256"])
            self.assertEqual(view.source_path, str(Path(parent["staged_path"])))
            self.assertTrue(view.source_verified)
            self.assertEqual(view.source_display_name, "grandmas-book.pdf")
            self.assertEqual(view.range_provenance["first_page"], 2)
            self.assertEqual(view.range_provenance["last_page"], 3)
            self.assertEqual(range_relative_to_original_page(view, 2), 3)
            with self.assertRaises(ValueError):
                range_relative_to_original_page(view, 3)

    def test_corrupt_or_missing_range_cache_cannot_register(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "output"
            source = root / "cookbook.pdf"
            _write_pdf(source, ["recipe"])
            recipe = {"name": "Cake", "ingredients": ["flour"], "instructions": ["bake"]}
            service, batch, parent, page_range, job_id = self._completed_range(
                output, source, 1, 1, [recipe], register=False,
            )
            cache_path = Path(service.range_snapshot(job_id)["ranges"][0]["cache_path"])
            cache_path.write_text("{}", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "cache is missing or invalid"):
                service.register_completed_range(
                    batch["batch_id"], parent["document_id"], page_range["range_id"], job_id=job_id,
                )
            self.assertEqual(load_catalog(output), [])

    def test_missing_parent_or_materialized_range_input_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "output"
            source = root / "cookbook.pdf"
            _write_pdf(source, ["recipe"])
            recipe = {"name": "Pie", "ingredients": ["fruit"], "instructions": ["bake"]}
            service, batch, parent, page_range, job_id = self._completed_range(
                output, source, 1, 1, [recipe], register=False,
            )
            Path(page_range["input_path"]).unlink()

            with self.assertRaisesRegex(ValueError, "range input"):
                service.register_completed_range(
                    batch["batch_id"], parent["document_id"], page_range["range_id"], job_id=job_id,
                )
            self.assertEqual(load_catalog(output), [])

    def test_changed_parent_after_registration_is_not_projected_or_reused(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "output"
            source = root / "cookbook.pdf"
            _write_pdf(source, ["recipe"])
            recipe = {"name": "Tart", "ingredients": ["fruit"], "instructions": ["bake"]}
            service, batch, parent, page_range, job_id = self._completed_range(
                output, source, 1, 1, [recipe],
            )
            service.register_completed_range(
                batch["batch_id"], parent["document_id"], page_range["range_id"], job_id=job_id,
            )
            Path(parent["staged_path"]).write_bytes(b"changed parent bytes")

            self.assertEqual(load_catalog(output), [])
            with self.assertRaisesRegex(ValueError, "range input"):
                service.register_completed_range(
                    batch["batch_id"], parent["document_id"], page_range["range_id"], job_id=job_id,
                )

    def test_partial_registration_recovers_without_replacing_existing_revision(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "output"
            source = root / "cookbook.pdf"
            _write_pdf(source, ["recipes"])
            recipes = [
                {"name": "A", "ingredients": ["x"], "instructions": ["y"]},
                {"name": "B", "ingredients": ["x"], "instructions": ["y"]},
            ]
            store = BatchStore(output)
            batch = store.create("Synthetic cookbook")
            parent = store.stage_parent_pdf(batch["batch_id"], source)
            page_range = store.create_page_range(batch["batch_id"], parent["document_id"], 1, 1)
            service = JobService(output, {"extractor_version": "extract-v1"},
                                 extractor=lambda *_: recipes)
            job = service.start_range_job(batch["batch_id"], [page_range["range_id"]], "local_ocr")
            review_path = output / "review_events.jsonl"
            original = ReviewStore._append_many

            def interrupt_after_one(store, events, *, locked=False):
                original(store, events[:1], locked=locked)
                raise OSError("simulated interrupted append")

            with patch.object(ReviewStore, "_append_many", interrupt_after_one):
                with self.assertRaisesRegex(OSError, "interrupted"):
                    service.run_range_pending(job["job_id"])
            before = ReviewStore(review_path)
            first_id = next(iter(before.registrations))
            first = before.registrations[first_id][0]
            saved = before.save_revision(
                candidate_id=first_id, source_hash=first["source_hash"],
                extraction_id=first["extraction_id"], expected_parent=0,
                operation_id="save-before-recovery", recipe=recipes[0],
                target_bindings=first["target_bindings"],
            )
            before.approve_revision(
                candidate_id=first_id, source_hash=first["source_hash"],
                extraction_id=first["extraction_id"], revision=saved["revision"],
                operation_id="approve-before-recovery", source_path=first["source"],
                review_rationale="Synthetic review fixture",
            )

            recovered = service.register_completed_ranges(job["job_id"])
            after = ReviewStore(review_path)

            self.assertEqual(len(recovered), 2)
            self.assertEqual(len(after.revisions[first_id]), 1)
            self.assertEqual(len(load_catalog(output)), 2)

    def test_changed_extraction_configuration_preserves_both_candidate_sets(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "output"
            source = root / "cookbook.pdf"
            _write_pdf(source, ["recipe"])
            store = BatchStore(output)
            batch = store.create("Versioned cookbook")
            parent = store.stage_parent_pdf(batch["batch_id"], source)
            page_range = store.create_page_range(batch["batch_id"], parent["document_id"], 1, 1)
            ids = []
            for version, title in (("v1", "Old name"), ("v2", "New name")):
                service = JobService(
                    output, {"extractor_version": version},
                    extractor=lambda *_, title=title: [{"name": title, "ingredients": ["x"],
                                                         "instructions": ["y"]}],
                )
                job = service.start_range_job(batch["batch_id"], [page_range["range_id"]], "local_ocr")
                service.run_range_pending(job["job_id"])
                ids.extend(item["candidate_id"] for item in service.register_completed_range(
                    batch["batch_id"], parent["document_id"], page_range["range_id"],
                    job_id=job["job_id"],
                ))

            self.assertEqual(len(set(ids)), 2)
            self.assertEqual({view.candidate.name for view in load_catalog(output)},
                             {"Old name", "New name"})

    def test_overlapping_ranges_preserve_same_recipe_as_independent_occurrences(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "output"
            source = root / "cookbook.pdf"
            _write_pdf(source, ["recipe begins", "recipe ends"])
            store = BatchStore(output)
            batch = store.create("Overlapping ranges")
            parent = store.stage_parent_pdf(batch["batch_id"], source)
            ranges = [store.create_page_range(batch["batch_id"], parent["document_id"], *bounds)
                      for bounds in ((1, 2), (2, 2))]
            recipe = {"name": "Pasta", "ingredients": ["noodles"], "instructions": ["boil"]}
            registered = []
            for page_range in ranges:
                service = JobService(output, {"extractor_version": "v1"}, extractor=lambda *_: [recipe])
                job = service.start_range_job(batch["batch_id"], [page_range["range_id"]], "local_ocr")
                service.run_range_pending(job["job_id"])
                registered.extend(service.register_completed_range(
                    batch["batch_id"], parent["document_id"], page_range["range_id"],
                    job_id=job["job_id"],
                ))

            self.assertEqual(len(registered), 2)
            self.assertNotEqual(registered[0]["candidate_id"], registered[1]["candidate_id"])
            self.assertEqual(len(load_catalog(output)), 2)

    def test_contradictory_candidate_occurrence_metadata_is_not_projected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "output"
            source = root / "cookbook.pdf"
            _write_pdf(source, ["recipe"])
            recipe = {"name": "Soup", "ingredients": ["water"], "instructions": ["heat"]}
            service, batch, parent, page_range, job_id = self._completed_range(
                output, source, 1, 1, [recipe],
            )
            registration = service.register_completed_range(
                batch["batch_id"], parent["document_id"], page_range["range_id"], job_id=job_id,
            )[0]
            review_path = output / "review_events.jsonl"
            text = review_path.read_text(encoding="utf-8").replace(
                '"range_occurrence": 0', '"range_occurrence": 3',
            )
            review_path.write_text(text, encoding="utf-8")

            self.assertEqual(load_catalog(output), [])

    def test_identical_range_input_hash_from_distinct_parent_books_stays_distinct(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "output"
            left_path, right_path = root / "left.pdf", root / "right.pdf"
            _write_pdf(left_path, ["different cover", "same recipe page"])
            _write_pdf(right_path, ["another cover", "same recipe page"])
            store = BatchStore(output)
            batch = store.create("Two parent books")
            parents = [store.stage_parent_pdf(batch["batch_id"], path)
                       for path in (left_path, right_path)]
            ranges = [store.create_page_range(batch["batch_id"], parent["document_id"], 2, 2)
                      for parent in parents]
            recipe = {"name": "Same recipe", "ingredients": ["x"], "instructions": ["y"]}
            registered = []
            for parent, page_range in zip(parents, ranges):
                service = JobService(output, {"extractor_version": "v1"}, extractor=lambda *_: [recipe])
                job = service.start_range_job(batch["batch_id"], [page_range["range_id"]], "local_ocr")
                service.run_range_pending(job["job_id"])
                registered.extend(service.register_completed_range(
                    batch["batch_id"], parent["document_id"], page_range["range_id"],
                    job_id=job["job_id"],
                ))

            self.assertEqual(len(registered), 2)
            self.assertNotEqual(registered[0]["candidate_id"], registered[1]["candidate_id"])
            self.assertEqual({item["source_hash"] for item in registered},
                             {parent["sha256"] for parent in parents})
            identical_bytes = dict(registered[0]["range_provenance"])
            identical_bytes["input_sha256"] = "a" * 64
            other_parent = dict(registered[1]["range_provenance"])
            other_parent["input_sha256"] = "a" * 64
            self.assertNotEqual(
                range_candidate_id(identical_bytes, "b" * 64, registered[0]["fingerprint"], 0),
                range_candidate_id(other_parent, "b" * 64, registered[1]["fingerprint"], 0),
            )


if __name__ == "__main__":
    unittest.main()
