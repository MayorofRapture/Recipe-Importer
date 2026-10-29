from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pymupdf as fitz

from import_recipes import INLINE_MAX_BYTES, extract
from model_routing import QuotaExhausted


class _FakeFiles:
    def __init__(self, *, delete_error=None):
        self.delete_error = delete_error
        self.calls = []

    def upload(self, **kwargs):
        self.calls.append(("upload", kwargs))
        return SimpleNamespace(name="files/fixture", uri="https://fixture.invalid/file")

    def delete(self, **kwargs):
        self.calls.append(("delete", kwargs))
        if self.delete_error:
            raise self.delete_error


class _FakeModels:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response


class _FakeClient:
    def __init__(self, responses, *, delete_error=None):
        self.files = _FakeFiles(delete_error=delete_error)
        self.models = _FakeModels(responses)


def _response():
    return SimpleNamespace(text='{"recipes": []}')


def _pdf(path: Path, *, text: str = "Recipe text") -> None:
    document = fitz.open()
    document.new_page().insert_text((72, 72), text)
    document.save(path)
    document.close()


class GeminiProviderAccountingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.path = self.root / "fixture.pdf"
        _pdf(self.path)

    def tearDown(self):
        self.temp.cleanup()

    def _client(self, *responses, delete_error=None):
        client = _FakeClient(responses or (_response(),), delete_error=delete_error)
        self.enterContext(patch("import_recipes.google_client", return_value=client))
        return client

    def test_small_pdf_uses_inline_input_and_one_generation_reservation(self):
        self.assertLessEqual(self.path.stat().st_size, INLINE_MAX_BYTES)
        client = self._client()
        reservations = []
        with patch("import_recipes.types.Part.from_bytes", return_value="inline-pdf") as inline:
            extract(self.path, "fixture-key", "model-a", before_request=lambda model, tokens=0:
                   reservations.append((model, tokens)))

        inline.assert_called_once()
        self.assertEqual(len(client.files.calls), 0)
        self.assertEqual(len(client.models.calls), 1)
        self.assertEqual(len(reservations), 1)

    def test_large_pdf_reserves_generation_only_and_deletes_uploaded_file(self):
        with self.path.open("ab") as handle:
            handle.write(b" " * (INLINE_MAX_BYTES + 1 - self.path.stat().st_size))
        self.assertGreater(self.path.stat().st_size, INLINE_MAX_BYTES)
        client = self._client()
        reservations = []

        extract(self.path, "fixture-key", "model-a", before_request=lambda model, tokens=0:
               reservations.append((model, tokens)))

        self.assertEqual([call[0] for call in client.files.calls], ["upload", "delete"])
        self.assertEqual(len(reservations), 1)

    def test_quota_exhaustion_after_upload_still_attempts_delete(self):
        with self.path.open("ab") as handle:
            handle.write(b" " * (INLINE_MAX_BYTES + 1 - self.path.stat().st_size))
        client = self._client()
        reservations = []

        def reserve_generation(model, tokens=0):
            reservations.append((model, tokens))
            raise QuotaExhausted("daily generation quota exhausted")

        with self.assertRaises(QuotaExhausted):
            extract(self.path, "fixture-key", "model-a", before_request=reserve_generation)

        self.assertEqual([call[0] for call in client.files.calls], ["upload", "delete"])
        self.assertEqual(len(reservations), 1)
        self.assertEqual(len(client.models.calls), 0)

    def test_missing_cloud_consent_blocks_large_pdf_upload_and_generation(self):
        with self.path.open("ab") as handle:
            handle.write(b" " * (INLINE_MAX_BYTES + 1 - self.path.stat().st_size))
        client = self._client()

        with self.assertRaisesRegex(ValueError, "consent"):
            extract(self.path, "fixture-key", "model-a", before_upload=lambda model:
                    (_ for _ in ()).throw(ValueError("Persisted cloud consent is missing")))

        self.assertEqual(client.files.calls, [])
        self.assertEqual(client.models.calls, [])

    def test_generation_error_still_deletes_uploaded_file(self):
        with self.path.open("ab") as handle:
            handle.write(b" " * (INLINE_MAX_BYTES + 1 - self.path.stat().st_size))
        client = self._client(RuntimeError("generation failed"))

        with self.assertRaisesRegex(RuntimeError, "generation failed"):
            extract(self.path, "fixture-key", "model-a")

        self.assertEqual([call[0] for call in client.files.calls], ["upload", "delete"])

    def test_cleanup_failure_is_attached_to_generation_error_without_secret(self):
        with self.path.open("ab") as handle:
            handle.write(b" " * (INLINE_MAX_BYTES + 1 - self.path.stat().st_size))
        client = self._client(RuntimeError("generation failed"),
                               delete_error=RuntimeError("cleanup failed fixture-secret"))

        with self.assertRaisesRegex(RuntimeError, "generation failed") as caught:
            extract(self.path, "fixture-secret", "model-a")

        self.assertIn("cleanup failed", str(caught.exception.diagnostics["cleanup_error"]))
        self.assertNotIn("fixture-secret", str(caught.exception.diagnostics))
        self.assertEqual([call[0] for call in client.files.calls], ["upload", "delete"])

    def test_cleanup_failure_after_success_is_a_batch_warning(self):
        with self.path.open("ab") as handle:
            handle.write(b" " * (INLINE_MAX_BYTES + 1 - self.path.stat().st_size))
        self._client(delete_error=RuntimeError("cleanup failed"))

        batch = extract(self.path, "fixture-key", "model-a")

        self.assertTrue(any("uploaded-file cleanup failed" in warning
                            for warning in batch.general_warnings))

    def test_generation_uses_last_daily_slot_and_cleanup_does_not_reserve_another(self):
        with self.path.open("ab") as handle:
            handle.write(b" " * (INLINE_MAX_BYTES + 1 - self.path.stat().st_size))
        client = self._client()
        reservations = []

        def reserve_with_two_request_daily_limit(model, tokens=0):
            reservations.append((model, tokens))
            if len(reservations) > 2:
                raise QuotaExhausted("daily generation quota exhausted")

        extract(self.path, "fixture-key", "model-a", before_request=reserve_with_two_request_daily_limit)

        self.assertEqual([call[0] for call in client.files.calls], ["upload", "delete"])
        self.assertEqual(len(reservations), 1)
        self.assertEqual(len(client.models.calls), 1)

    def test_each_internal_generation_retry_is_reserved_once(self):
        with self.path.open("ab") as handle:
            handle.write(b" " * (INLINE_MAX_BYTES + 1 - self.path.stat().st_size))
        client = self._client(RuntimeError("503 unavailable"), RuntimeError("503 unavailable"), _response())
        reservations = []

        with patch("import_recipes.time.sleep"), patch("import_recipes.random.uniform", return_value=0):
            extract(self.path, "fixture-key", "model-a", before_request=lambda model, tokens=0:
                   reservations.append((model, tokens)))

        self.assertEqual(len(client.models.calls), 3)
        self.assertEqual(len(reservations), 3)
        self.assertEqual([call[0] for call in client.files.calls], ["upload", "delete"])

    def test_pdf_token_estimate_ignores_binary_size_and_tracks_pages_and_text(self):
        from import_recipes import estimate_request_tokens

        one_page = estimate_request_tokens(self.path)
        with self.path.open("ab") as handle:
            handle.write(b" " * (INLINE_MAX_BYTES + 1 - self.path.stat().st_size))
        same_page_larger = estimate_request_tokens(self.path)

        self.assertEqual(one_page, same_page_larger)
        document = fitz.open(self.path)
        document.new_page().insert_text((72, 72), "A second known recipe page")
        second_page = self.root / "second-page.pdf"
        document.save(second_page)
        document.close()
        self.assertGreater(estimate_request_tokens(second_page), one_page)

    def test_scanned_pdf_uses_visual_page_budget_even_without_extractable_text(self):
        from import_recipes import estimate_request_tokens

        document = fitz.open()
        document.new_page()
        image_pdf = self.root / "image-only.pdf"
        document.save(image_pdf)
        document.close()

        two_page = fitz.open()
        two_page.new_page()
        two_page.new_page()
        two_page_pdf = self.root / "two-image-pages.pdf"
        two_page.save(two_page_pdf)
        two_page.close()
        self.assertGreater(estimate_request_tokens(two_page_pdf), estimate_request_tokens(image_pdf))


if __name__ == "__main__":
    unittest.main()
