import tempfile
import unittest
from pathlib import Path

from tests.ui_fixture_factory import write_ui_fixture
from ui_data import load_catalog, next_unresolved, render_source_page


class UiDataTests(unittest.TestCase):
    def test_catalog_projects_review_imported_and_pending_states(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = write_ui_fixture(Path(tmp))
            views = load_catalog(Path(fixture["output"]), Path(tmp, "sources"))

        by_name = {view.candidate.name: view for view in views}
        self.assertEqual(by_name["Fried Rice"].status, "Ready to review")
        self.assertEqual(by_name["The Best Caramel Apples"].status, "Needs attention")
        self.assertFalse(by_name["Cranberry Date Bars"].import_ready)
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
