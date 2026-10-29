import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import fitz
from PIL import Image

from tests.ui_fixture_factory import write_ui_fixture
from batch_store import BatchStore
from import_recipes import _ocr_text_paths, file_digest
from recipe_quality import ExtractionBatch, RecipeCandidate
from review_store import ReviewStore
from ui_data import (RecipeView, extraction_quality_audit, load_catalog, next_unresolved,
                     render_source_page, source_text_for_page)


class UiDataTests(unittest.TestCase):
    def _source_text_view(self, source: Path, digest: str | None = None, candidate_id: str = "synthetic-recipe") -> RecipeView:
        return RecipeView(
            candidate_id, digest or file_digest(source), str(source),
            RecipeCandidate(name=candidate_id, ingredients=["1 cup test flour"],
                            instructions=["Mix the synthetic ingredients."]),
            "Ready to review",
        )

    @staticmethod
    def _write_pdf(path: Path, pages: list[str]) -> None:
        document = fitz.open()
        for text in pages:
            page = document.new_page()
            if text:
                page.insert_text((72, 72), text)
        document.save(path)
        document.close()

    def test_source_text_uses_selectable_pdf_text_for_only_the_requested_page(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "two-pages.pdf"
            self._write_pdf(source, ["Alpha page one", "Beta page two"])
            output = root / "output"
            view = self._source_text_view(source)

            first = source_text_for_page(view, 1, output)
            second = source_text_for_page(view, 2, output)

            self.assertEqual(first.status, "available")
            self.assertEqual(first.method, "Selectable PDF text")
            self.assertIn("Alpha page one", first.text)
            self.assertNotIn("Beta page two", first.text)
            self.assertEqual((first.page_number, first.page_count), (1, 2))
            self.assertIn("Beta page two", second.text)
            self.assertNotIn("Alpha page one", second.text)
            self.assertEqual((second.page_number, second.page_count), (2, 2))

    def test_source_text_uses_only_the_hash_bound_ocr_page_for_scanned_pdf(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "scanned.pdf"
            self._write_pdf(source, ["", ""])
            output = root / "output"
            view = self._source_text_view(source)
            cache = _ocr_text_paths(output, view.source_hash)["selected"]
            cache.parent.mkdir(parents=True)
            cache.write_text("--- OCR PAGE 1 ---\nFirst scan page\n\n--- OCR PAGE 2 ---\nSecond scan page\n", encoding="utf-8")

            result = source_text_for_page(view, 2, output)

        self.assertEqual(result.status, "available")
        self.assertIn("Second scan page", result.text)
        self.assertNotIn("First scan page", result.text)
        self.assertEqual((result.page_number, result.page_count), (2, 2))
        self.assertTrue(any("OCR" in note for note in result.uncertainty))

    def test_source_text_accepts_unmarked_hash_bound_ocr_for_single_page_image(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "single.png"
            Image.new("RGB", (24, 24), "white").save(source)
            output = root / "output"
            view = self._source_text_view(source)
            cache = _ocr_text_paths(output, view.source_hash)["selected"]
            cache.parent.mkdir(parents=True)
            cache.write_text("Synthetic image transcription", encoding="utf-8")

            result = source_text_for_page(view, 1, output)

        self.assertEqual(result.status, "available")
        self.assertEqual(result.text, "Synthetic image transcription")
        self.assertEqual((result.page_number, result.page_count), (1, 1))
        self.assertIn("OCR", result.method)

    def test_source_text_rejects_unmarked_cache_for_multipage_pdf(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "scanned.pdf"
            self._write_pdf(source, ["", ""])
            output = root / "output"
            view = self._source_text_view(source)
            cache = _ocr_text_paths(output, view.source_hash)["selected"]
            cache.parent.mkdir(parents=True)
            cache.write_text("Text with no reliable page markers", encoding="utf-8")

            result = source_text_for_page(view, 1, output)

        self.assertEqual(result.status, "ambiguous_page")
        self.assertEqual(result.text, "")

    def test_source_text_rejects_duplicate_page_markers_as_ambiguous(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "scan.pdf"
            self._write_pdf(source, [""])
            output = root / "output"
            view = self._source_text_view(source)
            cache = _ocr_text_paths(output, view.source_hash)["selected"]
            cache.parent.mkdir(parents=True)
            cache.write_text("--- OCR PAGE 1 ---\nFirst section\n--- PDF PAGE 1 ---\nSecond section\n",
                             encoding="utf-8")

            result = source_text_for_page(view, 1, output)

        self.assertEqual(result.status, "ambiguous_page")
        self.assertEqual(result.text, "")

    def test_source_text_bounds_oversized_cache_reads(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "scan.png"
            Image.new("RGB", (24, 24), "white").save(source)
            output = root / "output"
            view = self._source_text_view(source)
            cache = _ocr_text_paths(output, view.source_hash)["selected"]
            cache.parent.mkdir(parents=True)
            cache.write_bytes(b"x" * (4 * 1024 * 1024 + 1))

            result = source_text_for_page(view, 1, output)

        self.assertEqual(result.status, "cache_unreadable")
        self.assertEqual(result.text, "")

    def test_source_text_rejects_cache_when_present_source_hash_mismatches(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "changed.pdf"
            self._write_pdf(source, ["Changed source bytes"])
            expected_hash = "a" * 64
            output = root / "output"
            cache = _ocr_text_paths(output, expected_hash)["selected"]
            cache.parent.mkdir(parents=True)
            cache.write_text("Must not be shown", encoding="utf-8")
            view = self._source_text_view(source, expected_hash)

            result = source_text_for_page(view, 1, output)

        self.assertEqual(result.status, "source_mismatch")
        self.assertEqual(result.text, "")

    def test_source_text_can_show_hash_bound_cache_when_original_is_missing_with_notice(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "output"
            digest = "b" * 64
            source = root / "missing.pdf"
            view = self._source_text_view(source, digest)
            cache = _ocr_text_paths(output, digest)["selected"]
            cache.parent.mkdir(parents=True)
            cache.write_text("--- OCR PAGE 1 ---\nCached page one\n", encoding="utf-8")

            result = source_text_for_page(view, 1, output)

        self.assertEqual(result.status, "available")
        self.assertIn("Cached page one", result.text)
        self.assertTrue(any("Original source is unavailable" in note for note in result.uncertainty))

    def test_source_text_reports_missing_and_unreadable_caches_without_raising(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "scan.pdf"
            self._write_pdf(source, [""])
            output = root / "output"
            view = self._source_text_view(source)
            missing = source_text_for_page(view, 1, output)
            cache = _ocr_text_paths(output, view.source_hash)["selected"]
            cache.parent.mkdir(parents=True)
            cache.write_bytes(b"\xff\xfe\xfa")

            unreadable = source_text_for_page(view, 1, output)

        self.assertEqual(missing.status, "no_text")
        self.assertEqual(unreadable.status, "cache_unreadable")
        self.assertEqual(unreadable.text, "")

    def test_source_text_uses_recorded_variant_when_selected_cache_is_missing(self):
        from import_recipes import OCR_STRUCTURE_CACHE_VERSION, OCR_STRUCTURE_PROMPT_VERSION

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "scan.pdf"
            self._write_pdf(source, [""])
            output = root / "output"
            view = self._source_text_view(source)
            text_dir = output / "ocr_text"
            text_dir.mkdir(parents=True)
            _ocr_text_paths(output, view.source_hash)["layout"].write_text(
                "--- OCR PAGE 1 ---\nLayout variant only", encoding="utf-8")
            extracted = output / "extracted"
            extracted.mkdir()
            metadata = {
                "schema_version": OCR_STRUCTURE_CACHE_VERSION,
                "prompt_version": OCR_STRUCTURE_PROMPT_VERSION,
                "source_hash": view.source_hash,
                "ocr_selected_variant": "layout",
                "ocr_selection_reason": ["Layout order selected for this scan."],
                "batch": {"recipes": []},
            }
            (extracted / f"{view.source_hash}--ocr-structure--local-1.json").write_text(
                json.dumps(metadata), encoding="utf-8")

            result = source_text_for_page(view, 1, output)

        self.assertEqual(result.status, "available")
        self.assertEqual(result.text, "Layout variant only")
        self.assertIn("layout", result.method.casefold())
        self.assertIn("Layout order selected for this scan.", result.uncertainty)

    def test_source_text_cache_is_bound_to_selected_candidate_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "output"
            views = []
            for name, content, color in (("first.jpg", "First candidate text", "white"),
                                          ("second.jpg", "Second candidate text", "black")):
                source = root / name
                Image.new("RGB", (24, 24), color).save(source)
                view = self._source_text_view(source, candidate_id=name)
                cache = _ocr_text_paths(output, view.source_hash)["selected"]
                cache.parent.mkdir(parents=True, exist_ok=True)
                cache.write_text(content, encoding="utf-8")
                views.append(view)

            first = source_text_for_page(views[0], 1, output)
            second = source_text_for_page(views[1], 1, output)

        self.assertEqual(first.text, "First candidate text")
        self.assertEqual(second.text, "Second candidate text")
        self.assertNotIn(first.text, second.text)

    def test_review_projection_exposes_shared_semantic_ingredient_findings(self):
        candidate = RecipeCandidate(
            ingredients=["Combine 1 cup flour and 2 eggs in a bowl."], instructions=[],
        )
        view = RecipeView("id", "hash", "", candidate, "Ready to review")
        self.assertTrue(any("preparation instruction" in item.explanation for item in view.quality_findings))

    def test_quality_audit_uses_effective_views_and_does_not_mutate_them(self):
        flagged = RecipeView("a", "hash", "", RecipeCandidate(
            ingredients=["Combine 1 cup flour and 2 eggs in a bowl."]
        ), "Needs attention")
        clean = RecipeView("b", "hash2", "", RecipeCandidate(
            ingredients=["1 cup chopped walnuts"], instructions=["Bake until set."]
        ), "Imported", protected=True)
        before = flagged.candidate.model_dump(mode="json")

        audit = extraction_quality_audit([flagged, clean])

        self.assertEqual((audit["inspected"], audit["with_issues"], audit["needs_manual_review"]), (2, 1, 1))
        self.assertEqual(flagged.candidate.model_dump(mode="json"), before)

    def test_quality_audit_counts_only_saved_corrections_and_durable_deferrals(self):
        from types import SimpleNamespace

        original = RecipeCandidate(ingredients=["Combine 1 cup flour and 2 eggs in a bowl."])
        corrected = RecipeView(
            "corrected", "hash", "", RecipeCandidate(ingredients=["1 cup flour", "2 eggs"],
                                                      instructions=["Combine in a bowl."]),
            "Ready to review", revision=1, source_candidate=original,
        )
        deferred = RecipeView(
            "deferred", "hash2", "", original, "Needs attention",
            draft=SimpleNamespace(repair_proposal={"status": "deferred"}),
        )

        audit = extraction_quality_audit([corrected, deferred])

        self.assertEqual(audit["corrected"], 1)
        self.assertEqual(audit["deferred"], 1)

    def test_cold_catalog_registers_all_sources_in_one_durable_batch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "output"
            extracted = output / "extracted"
            sources = root / "sources"
            extracted.mkdir(parents=True)
            sources.mkdir()
            for index in range(2):
                source = sources / f"source-{index}.jpg"
                source.write_bytes(f"synthetic source {index}".encode())
                batch = ExtractionBatch(recipes=[RecipeCandidate(
                    name=f"Recipe {index}", ingredients=["1 cup rice"], instructions=["Cook rice."],
                )])
                digest = file_digest(source)
                (extracted / f"{digest}.json").write_text(json.dumps({
                    "schema_version": 2, "source_hash": digest,
                    "batch": batch.model_dump(mode="json"),
                }), encoding="utf-8")

            with patch("review_store.os.fsync", wraps=os.fsync) as sync:
                views = load_catalog(output, sources)

        self.assertEqual(len(views), 2)
        self.assertEqual(sync.call_count, 1)

    def test_catalog_projects_review_imported_and_pending_states(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = write_ui_fixture(Path(tmp))
            views = load_catalog(Path(fixture["output"]), Path(tmp, "sources"))

        by_name = {view.candidate.name: view for view in views}
        self.assertEqual(by_name["Fried Rice"].status, "Ready to review")
        self.assertEqual(by_name["The Best Caramel Apples"].status, "Needs attention")
        self.assertFalse(by_name["Cranberry & Caramel Date Bars"].import_ready)
        self.assertEqual(by_name["Imported Recipe"].status, "Imported")
        self.assertTrue(by_name["Imported Recipe"].protected)

    def test_reordered_cache_candidates_keep_identity_and_import_protection(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = write_ui_fixture(Path(tmp))
            output = Path(fixture["output"])
            cache = next((output / "extracted").glob("*.json"))
            raw = cache.read_text(encoding="utf-8")
            data = __import__("json").loads(raw)
            data["batch"]["recipes"] = list(reversed(data["batch"]["recipes"]))
            cache.write_text(__import__("json").dumps(data, ensure_ascii=False), encoding="utf-8")
            views = load_catalog(output, Path(tmp, "sources"))

        imported = next(view for view in views if view.candidate.name == "Imported Recipe")
        self.assertTrue(imported.protected)
        self.assertEqual(imported.import_record["slug"], "imported-recipe")

    def test_source_preview_renders_pdf_page_without_modifying_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = write_ui_fixture(Path(tmp))
            source = Path(fixture["source"])
            before = source.stat().st_mtime_ns
            preview = render_source_page(source, 2)
            after = source.stat().st_mtime_ns

        self.assertEqual(preview.page_count, 2)
        self.assertEqual(preview.page_number, 2)
        self.assertIsNotNone(preview.image)
        self.assertEqual(before, after)

    def test_batch_staged_matching_bytes_restore_legacy_source_preview(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = write_ui_fixture(Path(tmp))
            output = Path(fixture["output"])
            source = Path(fixture["source"])
            content = source.read_bytes()
            store = BatchStore(output)
            batch = store.create("Source recovery")
            staged = store.stage(batch["batch_id"], source.name, content)
            source.unlink()

            views = load_catalog(output, Path(tmp, "sources"))
            fried = next(view for view in views if view.candidate.name == "Fried Rice")
            preview = render_source_page(Path(fried.source_path), 1)

        self.assertTrue(fried.source_available)
        self.assertEqual(fried.source_path, staged["staged_path"])
        self.assertIsNotNone(preview.image)

    def test_same_name_wrong_bytes_do_not_relink_or_enable_approval(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = write_ui_fixture(Path(tmp))
            output = Path(fixture["output"])
            source = Path(fixture["source"])
            source.unlink()
            wrong = Path(tmp, "sources", source.name)
            wrong.write_bytes(b"not the original recipe scan")

            views = load_catalog(output, Path(tmp, "sources"))
            fried = next(view for view in views if view.candidate.name == "Fried Rice")

        self.assertFalse(fried.source_available)
        self.assertNotEqual(fried.source_path, str(wrong))
        self.assertFalse(fried.import_ready)

    def test_catalog_load_observes_approval_written_by_another_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = write_ui_fixture(Path(tmp))
            output = Path(fixture["output"])
            before = load_catalog(output, Path(tmp, "sources"))
            fried = next(view for view in before if view.candidate.name == "Fried Rice")
            external = ReviewStore(output / "review_events.jsonl")
            external.save_revision(
                candidate_id=fried.candidate_id, source_hash=fried.source_hash,
                extraction_id=fried.extraction_id, expected_parent=0,
                operation_id="external-catalog-revision",
                recipe=fried.candidate.model_dump(mode="json"),
                target_bindings=list(fried.target_bindings),
            )
            external.approve_revision(
                candidate_id=fried.candidate_id, source_hash=fried.source_hash,
                extraction_id=fried.extraction_id, revision=1,
                operation_id="external-catalog-approval", source_path=fried.source_path,
            )

            after = load_catalog(output, Path(tmp, "sources"))
            refreshed = next(view for view in after if view.candidate_id == fried.candidate_id)

        self.assertEqual(refreshed.revision, 1)
        self.assertTrue(refreshed.import_ready)

    def test_catalog_rechecks_source_bytes_after_external_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = write_ui_fixture(Path(tmp))
            output = Path(fixture["output"])
            sources = Path(tmp, "sources")
            before = load_catalog(output, sources)
            fried = next(view for view in before if view.candidate.name == "Fried Rice")
            Path(fried.source_path).write_bytes(b"changed outside the catalog")

            after = load_catalog(output, sources)
            refreshed = next(view for view in after if view.candidate_id == fried.candidate_id)

        self.assertFalse(refreshed.source_available)
        self.assertFalse(refreshed.import_ready)

    def test_catalog_hashes_a_shared_source_once_per_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = write_ui_fixture(Path(tmp))
            source = Path(fixture["source"])
            from import_recipes import file_digest as real_file_digest
            calls = 0

            def count_source_digest(path):
                nonlocal calls
                if Path(path) == source:
                    calls += 1
                return real_file_digest(path)

            with patch("ui_data.file_digest", side_effect=count_source_digest):
                views = load_catalog(Path(fixture["output"]), Path(tmp, "sources"))

        self.assertEqual(len(views), 4)
        self.assertEqual(calls, 1)

    def test_catalog_observes_import_intent_written_after_prior_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = write_ui_fixture(Path(tmp))
            output = Path(fixture["output"])
            sources = Path(tmp, "sources")
            before = load_catalog(output, sources)
            fried = next(view for view in before if view.candidate.name == "Fried Rice")
            with (output / "ui_import_events.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({
                    "event": "intent_started", "attempt_id": "external-attempt",
                    "candidate_id": fried.candidate_id,
                }) + "\n")

            after = load_catalog(output, sources)
            refreshed = next(view for view in after if view.candidate_id == fried.candidate_id)

        self.assertTrue(refreshed.protected)
        self.assertEqual(refreshed.status, "Check Mealie")

    def test_next_unresolved_never_returns_imported_recipe(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = write_ui_fixture(Path(tmp))
            views = load_catalog(Path(fixture["output"]), Path(tmp, "sources"))
            first = next_unresolved(views, None)
            second = next_unresolved(views, first.candidate_id)

        self.assertNotEqual(first.status, "Imported")
        self.assertNotEqual(second.status, "Imported")


if __name__ == "__main__":
    unittest.main()
