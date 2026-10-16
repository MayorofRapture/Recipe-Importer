import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from streamlit.testing.v1 import AppTest

from runtime_safety import FixtureModeError, fixture_output_dir, fixture_source_allowed, require_live_services


class RuntimeSafetyTests(unittest.TestCase):
    def test_mealie_default_client_is_blocked_before_any_request(self):
        from mealie_client import MealieClient
        with patch.dict(os.environ, {"RECIPE_IMPORTER_FIXTURE_MODE": "1"}):
            with self.assertRaisesRegex(RuntimeError, "fixture"):
                MealieClient("http://example.invalid", "synthetic-token")

    def test_gemini_default_client_is_blocked_before_construction(self):
        from import_recipes import google_client
        with patch.dict(os.environ, {"RECIPE_IMPORTER_FIXTURE_MODE": "1"}):
            with self.assertRaisesRegex(RuntimeError, "fixture"):
                google_client("synthetic-token")

    def test_fixture_mode_refuses_live_services_even_with_credentials(self):
        with patch.dict(os.environ, {"RECIPE_IMPORTER_FIXTURE_MODE": "1",
                                     "MEALIE_TOKEN": "synthetic-token"}):
            with self.assertRaisesRegex(RuntimeError, "fixture"):
                require_live_services()

    def test_normal_mode_permits_explicit_service_construction(self):
        with patch.dict(os.environ, {"RECIPE_IMPORTER_FIXTURE_MODE": "0"}):
            require_live_services()

    def test_cli_import_is_blocked_in_fixture_mode_before_request(self):
        from import_recipes import import_mealie
        from recipe_quality import RecipeCandidate
        with patch.dict(os.environ, {"RECIPE_IMPORTER_FIXTURE_MODE": "1"}):
            with patch("import_recipes.requests.post") as post:
                with self.assertRaisesRegex(RuntimeError, "fixture"):
                    import_mealie(RecipeCandidate(name="Synthetic"), "http://example.invalid", "unused")
            post.assert_not_called()

    def test_fixture_output_requires_explicit_marked_isolated_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "fixture-output"
            output.mkdir()
            with patch.dict(os.environ, {"RECIPE_IMPORTER_FIXTURE_MODE": "1",
                                         "RECIPE_IMPORTER_OUTPUT_DIR": str(output)}, clear=False):
                with self.assertRaisesRegex(FixtureModeError, "marked"):
                    fixture_output_dir()
                (output / ".fixture-library").write_text("synthetic test library", encoding="utf-8")
                self.assertEqual(fixture_output_dir(), output.resolve())

    def test_fixture_mode_never_falls_back_to_default_output_directory(self):
        prior = os.environ.pop("RECIPE_IMPORTER_OUTPUT_DIR", None)
        try:
            with patch.dict(os.environ, {"RECIPE_IMPORTER_FIXTURE_MODE": "1"}, clear=False):
                with self.assertRaisesRegex(FixtureModeError, "explicit isolated output"):
                    fixture_output_dir()
        finally:
            if prior is not None:
                os.environ["RECIPE_IMPORTER_OUTPUT_DIR"] = prior

    def test_fixture_source_must_belong_to_marked_fixture_roots(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output, sources = root / "output", root / "sources"
            output.mkdir()
            sources.mkdir()
            (output / ".fixture-library").write_text("synthetic test library", encoding="utf-8")
            inside = sources / "recipe.pdf"
            inside.write_bytes(b"synthetic")
            outside = Path(tmp).parent / "unowned-source.pdf"
            with patch.dict(os.environ, {"RECIPE_IMPORTER_FIXTURE_MODE": "1",
                                         "RECIPE_IMPORTER_OUTPUT_DIR": str(output),
                                         "RECIPE_IMPORTER_SOURCE_DIR": str(sources)}, clear=False):
                self.assertTrue(fixture_source_allowed(inside))
                self.assertFalse(fixture_source_allowed(outside))

    def test_streamlit_fixture_without_output_stops_before_default_library_glob(self):
        app_path = Path(__file__).resolve().parents[1] / "streamlit_app.py"
        output_key = "RECIPE_IMPORTER_OUTPUT_DIR"
        previous_output = os.environ.pop(output_key, None)
        original_glob = Path.glob
        default_cache = (Path.cwd() / "output" / "extracted").resolve()

        def reject_default_cache(path, pattern):
            if path.resolve() == default_cache:
                raise AssertionError("fixture mode attempted to inspect default library")
            return original_glob(path, pattern)

        try:
            with patch.dict(os.environ, {"RECIPE_IMPORTER_FIXTURE_MODE": "1"}, clear=False):
                with patch.object(Path, "glob", reject_default_cache):
                    app = AppTest.from_file(str(app_path)).run(timeout=10)
        finally:
            if previous_output is not None:
                os.environ[output_key] = previous_output
        self.assertFalse(app.exception)
        self.assertTrue(any("explicit isolated output" in item.value for item in app.error))
        self.assertFalse(any(item.label == "Recipe title" for item in app.text_input))
