import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from local_ocr import OcrResult, OcrUnavailable, extract_local, _layout_order


class _FakeImage:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    @staticmethod
    def open(source):
        return _FakeImage()

    def close(self):
        pass


class _FakeTesseract:
    def __init__(self, text):
        self.text = text
        self.pytesseract = SimpleNamespace(tesseract_cmd="")

    def image_to_string(self, image):
        return self.text


class _FakeLayoutTesseract:
    def __init__(self, data, text="fallback"):
        self.data = data
        self.text = text
        self.pytesseract = SimpleNamespace(
            tesseract_cmd="", Output=SimpleNamespace(DICT="dict"),
        )

    def image_to_data(self, image, output_type=None):
        return self.data

    def image_to_string(self, image):
        return self.text


class _FakePage:
    def __init__(self, text="", image_bytes=b"png", dict_data=None):
        self.text = text
        self.image_bytes = image_bytes
        self.dict_data = dict_data

    def get_text(self, mode):
        if mode == "dict" and self.dict_data is not None:
            return self.dict_data
        return self.text

    def get_pixmap(self, matrix=None, alpha=False):
        return SimpleNamespace(tobytes=lambda kind: self.image_bytes)


class _FakeDocument:
    def __init__(self, pages):
        self.pages = pages

    def __len__(self):
        return len(self.pages)

    def __getitem__(self, index):
        return self.pages[index]


class LocalOcrTests(unittest.TestCase):
    def test_one_column_layout_does_not_sort_steps_by_small_x_differences(self):
        lines = [
            SimpleNamespace(text="Step 1", x0=100, y0=100, x1=180, y1=120,
                            page=1, source="tesseract-block:1"),
            SimpleNamespace(text="Step 2", x0=90, y0=200, x1=170, y1=220,
                            page=1, source="tesseract-block:2"),
            SimpleNamespace(text="Step 3", x0=110, y0=300, x1=190, y1=320,
                            page=1, source="tesseract-block:3"),
        ]

        ordered = _layout_order(lines)

        self.assertEqual([line.text for line in ordered], ["Step 1", "Step 2", "Step 3"])

    def test_carameled_apples_prefers_original_when_layout_steps_are_out_of_order(self):
        original = (
            "allrecipes’ 09 prea\n"
            "The Best Caramel Apples\n"
            "Ingredients\n"
            "8 large tart apples\n"
            "2 cups packed brown sugar\n"
            "Directions\n"
            "Step 1\nBring apples to a boil.\n"
            "Step 2\nInsert sticks.\n"
            "Step 3\nLine a baking sheet.\n"
            "Step 4\nCook the caramel.\n"
            "Step 5\nDip each apple."
        )
        layout = (
            "allrecipes’ 09 prea\n"
            "Step 5\nDip each apple.\n"
            "Step 4\nCook the caramel.\n"
            "Step 2\nInsert sticks.\n"
            "Step 3\nLine a baking sheet.\n"
            "Step 1\nBring apples to a boil.\n"
            "The Best Caramel Apples\nIngredients\n"
            "8 large tart apples\n2 cups packed brown sugar\nDirections"
        )

        result = OcrResult.from_variants(original, layout)
        candidate = result.batch.recipes[0]

        self.assertEqual(result.selected_variant, "original")
        self.assertEqual(candidate.name, "The Best Caramel Apples")
        self.assertEqual([step.split()[1] for step in candidate.instructions], ["1", "2", "3", "4", "5"])
        self.assertNotEqual(candidate.confidence, "high")
        self.assertTrue(any("layout" in warning.casefold() for warning in result.uncertainties))

    def test_date_bars_layout_mixing_is_flagged_instead_of_high_confidence(self):
        original = (
            "Cranberry & Caramel Date Bars\n"
            "1 cup cranberries\n"
            "% cup plus 2 tablespoons sugar\n"
            "2% cups all purpose flour\n"
            "Heat oven to 350°F. In bowl, combine cranberries and sugar.\n"
            "Bake 20 minutes or until lightly brown."
        )
        layout = (
            "1 cup cranberries Heat oven to 350°F. In bowl, combine\n"
            "% cup plus 2 tablespoons sugar cranberries and sugar.\n"
            "2% cups all purpose flour Bake 20 minutes.\n"
            "Cranberry & Caramel Date Bars"
        )

        result = OcrResult.from_variants(original, layout)
        candidate = result.batch.recipes[0]

        self.assertTrue(candidate.requires_review)
        self.assertNotEqual(candidate.confidence, "high")
        self.assertTrue(any("mixed" in item.detail.casefold()
                            or "fragment" in item.detail.casefold()
                            for item in candidate.missing_information))

    def test_website_header_is_not_title_and_multiline_title_is_preserved(self):
        text = (
            "allrecipes’ 09 prea\n"
            "Search Log In Sign Up\n"
            "The Best\n"
            "Caramel Apples\n"
            "INGREDIENTS:\n"
            "8 apples\n"
            "2 cups sugar\n"
            "1/2 cup water\n"
            "1/2 cup corn syrup\n"
            "1 teaspoon vanilla\n"
            "1/4 teaspoon salt\n"
            "1 tablespoon butter\n"
            "1 cup chopped nuts\n"
            "INSTRUCTIONS:\n"
            "1. Dip each apple in the caramel."
        )

        result = OcrResult.from_text(text)
        candidate = result.batch.recipes[0]

        self.assertEqual(candidate.name, "The Best Caramel Apples")
        self.assertEqual(len(candidate.ingredients), 8)
        self.assertNotIn("allrecipes", " ".join(candidate.ingredients).casefold())
        self.assertTrue(candidate.requires_review)

    def test_tesseract_bounding_boxes_order_two_columns_left_to_right(self):
        data = {
            "text": [
                "2", "cups", "sugar", "1", "cup", "water",
                "1.", "Combine", "sugar", "and", "water.",
                "2.", "Boil", "until", "clear.",
            ],
            "left": [10, 22, 44, 10, 22, 38, 320, 332, 370, 405, 440, 320, 332, 365, 400],
            "top": [10, 10, 10, 32, 32, 32, 10, 10, 10, 10, 10, 32, 32, 32, 32],
            "width": [8, 15, 30, 8, 15, 28, 12, 42, 35, 25, 42, 12, 32, 25, 35],
            "height": [12] * 15,
            "block_num": [1] * 15,
            "par_num": [1] * 15,
            "line_num": [1] * 3 + [2] * 3 + [1] * 5 + [2] * 4,
            "conf": ["95"] * 15,
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "Scan_20260907_161736.jpg"
            path.write_bytes(b"fixture")
            with patch("local_ocr._load_tesseract",
                       return_value=_FakeLayoutTesseract(data)), \
                 patch("local_ocr._load_pil", return_value=_FakeImage):
                result = extract_local(path)

        self.assertEqual(result.text.splitlines(), [
            "2 cups sugar", "1 cup water",
            "1. Combine sugar and water.", "2. Boil until clear.",
        ])

    def test_pdf_positional_text_keeps_page_markers_out_of_recipe_fields(self):
        page_one = _FakePage("interleaved text", dict_data={
            "blocks": [{"type": 0, "bbox": [300, 10, 500, 40], "lines": [
                {"bbox": [300, 10, 500, 40], "spans": [{"text": "1. Stir the sauce."}]},
            ]}, {"type": 0, "bbox": [10, 10, 200, 40], "lines": [
                {"bbox": [10, 10, 200, 40], "spans": [{"text": "INGREDIENTS:"}]},
                {"bbox": [10, 50, 200, 80], "spans": [{"text": "1 cup sauce"}]},
            ]}],
        })
        page_two = _FakePage("interleaved page two", dict_data={
            "blocks": [{"type": 0, "bbox": [10, 10, 200, 40], "lines": [
                {"bbox": [10, 10, 200, 40], "spans": [{"text": "2. Serve warm."}]},
            ]}],
        })
        fitz = SimpleNamespace(
            Matrix=lambda x, y: (x, y),
            open=lambda source: _FakeDocument([page_one, page_two]),
        )

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "Scan_20260907_161607(1).pdf"
            path.write_bytes(b"fixture")
            with patch("local_ocr._load_pymupdf", return_value=fitz):
                result = extract_local(path)

        self.assertIn("--- PDF PAGE 1 ---", result.text)
        self.assertIn("--- PDF PAGE 2 ---", result.text)
        self.assertLess(result.text.index("INGREDIENTS:"), result.text.index("1. Stir"))
        self.assertLess(result.text.index("1. Stir"), result.text.index("2. Serve"))
        candidate = result.batch.recipes[0]
        self.assertNotIn("PDF PAGE", candidate.name)
        self.assertTrue(any(item.field_path == "name" for item in candidate.missing_information))
        self.assertTrue(candidate.requires_review)

    def test_page_markers_and_stray_number_punctuation_remain_reviewable(self):
        text = (
            "--- OCR PAGE 1 ---\n"
            "Scan_20260907_164230(1).pdf\n"
            "INGREDIENTS:\n"
            "1 � cups flour\n"
            "INSTRUCTIONS:\n"
            "1) Mix the flour.\n"
            "2 . Bake until done.\n"
            "--- OCR PAGE 2 ---\n"
            "3) Cool before serving."
        )

        result = OcrResult.from_text(text)
        candidate = result.batch.recipes[0]

        self.assertNotIn("OCR PAGE", candidate.name)
        self.assertEqual(candidate.instructions, [
            "1) Mix the flour.", "2 . Bake until done.", "3) Cool before serving.",
        ])
        self.assertTrue(any(item.field_path.startswith("ingredients[")
                            for item in candidate.missing_information))
        self.assertTrue(candidate.requires_review)

    def test_title_detection_skips_scan_headers_timestamps_and_page_numbers(self):
        text = (
            "Scan_20260907_112806.jpg\n"
            "Page 1 of 2\n"
            "1/18/2014 8:52:00 PM\n\n"
            "Fried Rice\n\n"
            "INGREDIENTS:\n"
            "2 cups rice\n"
            "INSTRUCTIONS:\n"
            "Stir-fry rice"
        )

        result = OcrResult.from_text(text)

        self.assertEqual(result.batch.recipes[0].name, "Fried Rice")
        self.assertEqual(result.text, text)
        self.assertTrue(result.manual_review_required)

    def test_numeric_recipe_titles_are_not_treated_as_scan_metadata(self):
        text = "Page 2\n2024 7-Layer Dip\nINGREDIENTS:\nchips\nINSTRUCTIONS:\nServe"

        result = OcrResult.from_text(text)

        self.assertEqual(result.batch.recipes[0].name, "2024 7-Layer Dip")
        self.assertEqual(result.text, text)

    def test_jpg_ocr_preserves_text_and_requires_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "scan.jpg"
            path.write_bytes(b"fixture")
            tesseract = _FakeTesseract(
                "RECIPE 1: Jalapeno Peppers\nIngredients:\n2 peppers\nInstructions:\nRoast peppers"
            )
            with patch("local_ocr._load_tesseract", return_value=tesseract), \
                 patch("local_ocr._load_pil", return_value=_FakeImage):
                result = extract_local(path, "C:/Tesseract/tesseract.exe")

        self.assertEqual(result.engine, "tesseract")
        self.assertEqual(tesseract.pytesseract.tesseract_cmd, "C:/Tesseract/tesseract.exe")
        self.assertIn("Jalapeno Peppers", result.text)
        self.assertEqual([candidate.name for candidate in result.batch.recipes], ["Jalapeno Peppers"])
        self.assertTrue(result.batch.recipes[0].missing_information)
        self.assertTrue(result.manual_review_required)

    def test_selectable_pdf_uses_pymupdf_without_tesseract(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "recipes.pdf"
            path.write_bytes(b"fixture")
            fitz = SimpleNamespace(open=lambda source: _FakeDocument([
                _FakePage("Recipe title\nIngredients:\n1 cup flour\nInstructions:\nMix")
            ]))
            with patch("local_ocr._load_pymupdf", return_value=fitz), \
                 patch("local_ocr._load_tesseract", side_effect=AssertionError("not needed")):
                result = extract_local(path)

        self.assertEqual(result.engine, "pymupdf-text")
        self.assertIn("1 cup flour", result.text)

    def test_scanned_pdf_uses_tesseract_for_each_page(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "scanned.pdf"
            path.write_bytes(b"fixture")
            fitz = SimpleNamespace(
                Matrix=lambda x, y: (x, y),
                open=lambda source: _FakeDocument([_FakePage(""), _FakePage("")]),
            )
            tesseract = _FakeTesseract("RECIPE 1: First\nIngredients:\na\nInstructions:\nb")
            with patch("local_ocr._load_pymupdf", return_value=fitz), \
                 patch("local_ocr._load_tesseract", return_value=tesseract), \
                 patch("local_ocr._load_pil", return_value=_FakeImage):
                result = extract_local(path)

        self.assertEqual(result.engine, "pymupdf-text+tesseract")
        self.assertEqual(result.text.count("RECIPE 1: First"), 2)

    def test_missing_tesseract_is_explicit(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "scan.jpg"
            path.write_bytes(b"fixture")
            with patch("local_ocr._load_tesseract", side_effect=OcrUnavailable("Tesseract is not installed")), \
                 patch("local_ocr._load_pil", return_value=_FakeImage):
                with self.assertRaises(OcrUnavailable):
                    extract_local(path)

    def test_marker_separated_ocr_text_keeps_multiple_candidates(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "scan.png"
            path.write_bytes(b"fixture")
            tesseract = _FakeTesseract(
                "RECIPE 1: Salsa\nIngredients:\ntomato\nInstructions:\nChop\n"
                "RECIPE 2: Dip\nIngredients:\nyogurt\nInstructions:\nStir"
            )
            with patch("local_ocr._load_tesseract", return_value=tesseract), \
                 patch("local_ocr._load_pil", return_value=_FakeImage):
                result = extract_local(path)

        self.assertEqual([candidate.name for candidate in result.batch.recipes], ["Salsa", "Dip"])
        self.assertTrue(all(candidate.missing_information for candidate in result.batch.recipes))
        self.assertTrue(result.manual_review_required)


if __name__ == "__main__":
    unittest.main()
