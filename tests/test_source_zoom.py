import tempfile
import unittest
from pathlib import Path

from tests.ui_fixture_factory import write_ui_fixture
from ui_data import render_source_page


class SourceZoomTests(unittest.TestCase):
    def test_zoom_renders_more_detail_without_changing_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(write_ui_fixture(Path(tmp))["source"])
            original = source.read_bytes()
            normal = render_source_page(source, 1)
            zoomed = render_source_page(source, 1, zoom=2)
            self.assertGreater(zoomed.image.width, normal.image.width)
            self.assertEqual(zoomed.page_count, 2)
            self.assertEqual(source.read_bytes(), original)

    def test_out_of_bounds_page_preserves_real_page_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(write_ui_fixture(Path(tmp))["source"])
            result = render_source_page(source, 3, zoom=1)
            self.assertIsNone(result.image)
            self.assertEqual(result.page_count, 2)
