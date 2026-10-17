import tempfile
import unittest
import os
from pathlib import Path

import fitz
from PIL import Image
from tests.ui_fixture_factory import write_ui_fixture
from ui_data import render_source_page


_TEST_TMP_ROOT = Path(__file__).resolve().parents[1] / ".test-tmp" / "source-zoom"


def _temporary_directory():
    _TEST_TMP_ROOT.mkdir(parents=True, exist_ok=True)
    return tempfile.TemporaryDirectory(dir=_TEST_TMP_ROOT)


def _write_pdf(path: Path, width: int, height: int) -> None:
    document = fitz.open()
    page = document.new_page(width=width, height=height)
    page.insert_text((72, 72), "Synthetic preview", fontsize=18)
    document.save(path)
    document.close()


class SourceZoomTests(unittest.TestCase):
    def test_zoom_tiers_cap_preview_pixels_and_cache_tracks_source_change(self):
        with _temporary_directory() as tmp:
            source = Path(tmp) / "large.pdf"
            _write_pdf(source, 1800, 2400)
            original = source.read_bytes()
            fit = render_source_page(source, 1, zoom=1)
            inspect = render_source_page(source, 1, zoom=1.5)
            detail = render_source_page(source, 1, zoom=2)
            self.assertEqual(fit.image.size, (1200, 1600))
            self.assertEqual(inspect.image.size, (1800, 2400))
            self.assertEqual(detail.image.size, (2400, 3200))
            self.assertEqual(source.read_bytes(), original)

            previous_mtime = source.stat().st_mtime_ns
            _write_pdf(source, 600, 800)
            os.utime(source, ns=(previous_mtime + 1_000_000, previous_mtime + 1_000_000))
            resized_source = source.read_bytes()
            updated = render_source_page(source, 1, zoom=1)
            self.assertEqual(updated.image.size, (900, 1200))
            self.assertEqual(source.read_bytes(), resized_source)

    def test_out_of_bounds_page_preserves_real_page_count(self):
        with _temporary_directory() as tmp:
            source = Path(write_ui_fixture(Path(tmp))["source"])
            result = render_source_page(source, 3, zoom=1)
            self.assertIsNone(result.image)
            self.assertEqual(result.page_count, 2)

    def test_jpeg_zoom_tiers_downsample_without_modifying_source(self):
        with _temporary_directory() as tmp:
            source = Path(tmp) / "large.jpg"
            Image.new("RGB", (2400, 3200), "white").save(source, quality=90)
            original = source.read_bytes()

            fit = render_source_page(source, 1, zoom=1)
            inspect = render_source_page(source, 1, zoom=1.5)
            detail = render_source_page(source, 1, zoom=2)

            self.assertEqual(fit.image.size, (1200, 1600))
            self.assertEqual(inspect.image.size, (1800, 2400))
            self.assertEqual(detail.image.size, (2400, 3200))
            self.assertEqual(source.read_bytes(), original)
