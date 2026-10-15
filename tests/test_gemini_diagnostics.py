import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from import_recipes import GeminiExtractionError, StateStore, extract, process_file
from model_routing import ModelConfig, ModelRouter, UsageLedger


class _FakeModels:
    def __init__(self, response):
        self.response = response

    def generate_content(self, **kwargs):
        return self.response


class _FakeClient:
    def __init__(self, response):
        self.models = _FakeModels(response)


def _response(*, text="", candidates=None, prompt_feedback=None, usage_metadata=None):
    return SimpleNamespace(
        text=text,
        candidates=candidates,
        prompt_feedback=prompt_feedback,
        usage_metadata=usage_metadata,
    )


class GeminiDiagnosticsTests(unittest.TestCase):
    def _extract_empty(self, response):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "scan.jpg"
            path.write_bytes(b"fixture")
            with patch("import_recipes.google_client", return_value=_FakeClient(response)), \
                 patch("import_recipes.types.Part.from_bytes", return_value=object()):
                with self.assertRaises(GeminiExtractionError) as caught:
                    extract(path, "api-key", "gemini-test")
        return caught.exception

    def test_max_tokens_empty_response_captures_response_metadata(self):
        error = self._extract_empty(_response(
            candidates=[SimpleNamespace(
                finish_reason="MAX_TOKENS",
                safety_ratings=[SimpleNamespace(category="HARM_CATEGORY_SAFE", probability="NEGLIGIBLE")],
            )],
            prompt_feedback=SimpleNamespace(block_reason=None),
            usage_metadata=SimpleNamespace(
                prompt_token_count=12, candidates_token_count=99, total_token_count=111,
            ),
        ))

        self.assertEqual(error.diagnostics["reason"], "MAX_TOKENS")
        self.assertEqual(error.diagnostics["candidate_count"], 1)
        self.assertEqual(error.diagnostics["finish_reason"], "MAX_TOKENS")
        self.assertEqual(error.diagnostics["usage_metadata"]["total_token_count"], 111)
        self.assertEqual(error.model, "gemini-test")

    def test_safety_empty_response_is_distinguished(self):
        error = self._extract_empty(_response(
            candidates=[],
            prompt_feedback=SimpleNamespace(
                block_reason="SAFETY",
                safety_ratings=[SimpleNamespace(category="HARM_CATEGORY_DANGEROUS_CONTENT", probability="HIGH")],
            ),
        ))

        self.assertEqual(error.diagnostics["reason"], "SAFETY")
        self.assertEqual(error.diagnostics["candidate_count"], 0)
        self.assertEqual(error.diagnostics["prompt_feedback"]["block_reason"], "SAFETY")

    def test_recitation_empty_response_is_distinguished(self):
        error = self._extract_empty(_response(
            candidates=[SimpleNamespace(finish_reason="RECITATION", safety_ratings=[])],
        ))

        self.assertEqual(error.diagnostics["reason"], "RECITATION")
        self.assertEqual(error.diagnostics["finish_reason"], "RECITATION")

    def test_missing_candidates_and_other_empty_response_are_distinguished(self):
        missing = self._extract_empty(_response(candidates=None))
        other = self._extract_empty(_response(
            candidates=[SimpleNamespace(finish_reason="STOP", safety_ratings=[])],
        ))

        self.assertEqual(missing.diagnostics["reason"], "MISSING_CANDIDATES")
        self.assertEqual(other.diagnostics["reason"], "OTHER")

    def test_report_and_local_log_record_model_route_and_diagnostics(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "scan.jpg"
            cache = root / "extracted"
            cache.mkdir()
            error = GeminiExtractionError(
                "Gemini returned no text (MAX_TOKENS)",
                model="gemini-3.7-flash",
                diagnostics={
                    "reason": "MAX_TOKENS", "candidate_count": 1,
                    "finish_reason": "MAX_TOKENS",
                },
            )
            state = StateStore(root / "state.jsonl")

            with patch("import_recipes.extract", side_effect=error):
                rows = process_file(
                    source, "hash", {"api_key": "", "mealie_url": "http://mealie",
                                     "mealie_token": "token", "model": "gemini-3.7-flash"},
                    None, state, cache, False, force_extract=True,
                )

            diagnostic_log = root / "gemini_diagnostics.jsonl"
            logged = json.loads(diagnostic_log.read_text(encoding="utf-8").splitlines()[0])

        self.assertEqual(rows[0]["status"], "EXTRACTION_FAILED")
        self.assertEqual(rows[0]["model"], "gemini-3.7-flash")
        self.assertEqual(rows[0]["route_reason"], "primary")
        self.assertEqual(rows[0]["diagnostic_reason"], "MAX_TOKENS")
        self.assertEqual(logged["model"], "gemini-3.7-flash")
        self.assertEqual(logged["route_reason"], "primary")
        self.assertEqual(logged["diagnostics"]["candidate_count"], 1)

    def test_router_preserves_actual_last_model_and_attempt_sequence_on_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            router = ModelRouter([
                ModelConfig("gemini-3.5-flash-lite", "primary"),
                ModelConfig("gemini-3.7-flash", "quality_escalation"),
            ], UsageLedger(Path(tmp) / "usage.json"), sleep=lambda _: None)

            with self.assertRaises(GeminiExtractionError) as caught:
                router.run(lambda model: (_ for _ in ()).throw(GeminiExtractionError(
                    "Gemini returned no text (MAX_TOKENS)", model=model.id,
                    diagnostics={"reason": "MAX_TOKENS"},
                )))

        self.assertEqual(caught.exception.model, "gemini-3.7-flash")
        self.assertEqual(caught.exception.route_reason, "quality_escalation")
        self.assertEqual(caught.exception.attempted_models, [
            "gemini-3.5-flash-lite", "gemini-3.7-flash",
        ])

    def test_router_does_not_rotate_after_recitation(self):
        with tempfile.TemporaryDirectory() as tmp:
            router = ModelRouter([
                ModelConfig("gemini-3.5-flash-lite", "primary"),
                ModelConfig("gemini-3.7-flash", "quality_escalation"),
            ], UsageLedger(Path(tmp) / "usage.json"), sleep=lambda _: None)
            calls = []

            def attempt(model):
                calls.append(model.id)
                raise GeminiExtractionError(
                    "Gemini returned no text (RECITATION)", model=model.id,
                    diagnostics={"reason": "RECITATION", "finish_reason": "RECITATION"},
                )

            with self.assertRaises(GeminiExtractionError) as caught:
                router.run(attempt)

        self.assertEqual(calls, ["gemini-3.5-flash-lite"])
        self.assertEqual(caught.exception.attempted_models, ["gemini-3.5-flash-lite"])
        self.assertEqual(caught.exception.route_reason, "primary")

    def test_recitation_fallback_enters_review_without_importing(self):
        from local_ocr import OcrResult

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "scan.jpg"
            source.write_bytes(b"fixture")
            cache = root / "extracted"
            cache.mkdir()
            state = StateStore(root / "state.jsonl")
            batch = OcrResult.from_text(
                "RECIPE 1: Jalapeno Peppers\nIngredients:\n2 peppers\nInstructions:\nRoast peppers"
            )
            error = GeminiExtractionError(
                "Gemini returned no text (RECITATION)", model="gemini-3.5-flash-lite",
                diagnostics={"reason": "RECITATION", "finish_reason": "RECITATION",
                             "candidate_count": 1},
            )

            with patch("import_recipes.extract", side_effect=error), \
                 patch("import_recipes.local_ocr.extract_local", return_value=batch), \
                 patch("import_recipes.import_mealie") as mealie_import:
                rows = process_file(
                    source, "hash", {"api_key": "", "mealie_url": "http://mealie",
                                     "mealie_token": "token", "model": "gemini-3.5-flash-lite"},
                    None, state, cache, False, force_extract=True,
                )

            report = rows[0]
            saved_ocr = Path(report["ocr_text_path"]).read_text(encoding="utf-8")
            diagnostic = json.loads((root / "gemini_diagnostics.jsonl").read_text(encoding="utf-8").splitlines()[0])

        self.assertEqual(report["status"], "NEEDS_REVIEW")
        self.assertEqual(report["diagnostic_reason"], "RECITATION")
        self.assertEqual(report["ocr_attempted"], True)
        self.assertEqual(report["manual_review_required"], True)
        self.assertIn("Jalapeno Peppers", saved_ocr)
        self.assertEqual(diagnostic["diagnostics"]["reason"], "RECITATION")
        self.assertEqual(diagnostic["model"], "gemini-3.5-flash-lite")
        self.assertEqual(diagnostic["route_reason"], "primary")
        self.assertTrue(diagnostic["ocr_attempted"])
        mealie_import.assert_not_called()


if __name__ == "__main__":
    unittest.main()
