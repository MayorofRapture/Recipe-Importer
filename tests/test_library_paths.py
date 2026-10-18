import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from library_paths import LibraryPathConflict, alternate_cwd_library, resolve_library_path


class LibraryPathTests(unittest.TestCase):
    def test_default_is_application_relative_independent_of_current_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Recipe Importer"
            cwd = Path(tmp) / "elsewhere"
            root.mkdir()
            cwd.mkdir()
            with patch.dict(os.environ, {"RECIPE_IMPORTER_OUTPUT_DIR": ""}, clear=False), \
                 patch("pathlib.Path.cwd", return_value=cwd):
                selected = resolve_library_path(app_root=root)

        self.assertEqual(selected, root.resolve() / "output")

    def test_environment_relative_path_is_anchored_to_application(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "app"
            root.mkdir()
            with patch.dict(os.environ, {"RECIPE_IMPORTER_OUTPUT_DIR": "personal library"}, clear=False):
                selected = resolve_library_path(app_root=root)

        self.assertEqual(selected, (root / "personal library").resolve())

    def test_explicit_relative_path_uses_cwd_and_precedes_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "app"
            cwd = Path(tmp) / "launch folder"
            root.mkdir()
            cwd.mkdir()
            with patch.dict(os.environ, {"RECIPE_IMPORTER_OUTPUT_DIR": "environment"}, clear=False), \
                 patch("pathlib.Path.cwd", return_value=cwd):
                selected = resolve_library_path("explicit library", app_root=root)

        self.assertEqual(selected, (cwd / "explicit library").resolve())

    def test_default_fails_closed_when_launch_directory_has_an_existing_library(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "app"
            cwd = Path(tmp) / "launch"
            root.mkdir()
            (cwd / "output").mkdir(parents=True)
            (cwd / "output" / "review_events.jsonl").write_text("synthetic\n", encoding="utf-8")
            with patch.dict(os.environ, {"RECIPE_IMPORTER_OUTPUT_DIR": ""}, clear=False), \
                 patch("pathlib.Path.cwd", return_value=cwd):
                with self.assertRaises(LibraryPathConflict):
                    resolve_library_path(app_root=root)

    def test_fixture_library_overrides_explicit_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = root / "synthetic fixture"
            fixture.mkdir()
            (fixture / ".fixture-library").write_text("synthetic fixture library", encoding="utf-8")
            with patch.dict(os.environ, {
                "RECIPE_IMPORTER_FIXTURE_MODE": "1",
                "RECIPE_IMPORTER_OUTPUT_DIR": str(fixture),
            }, clear=False):
                selected = resolve_library_path(root / "production", app_root=root)

        self.assertEqual(selected, fixture.resolve())

    def test_alternate_existing_cwd_library_is_reportable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            active = root / "chosen"
            cwd = root / "launch"
            active.mkdir()
            (cwd / "output").mkdir(parents=True)
            (cwd / "output" / "import_state.jsonl").write_text("synthetic\n", encoding="utf-8")
            self.assertEqual(alternate_cwd_library(active, cwd), cwd / "output")


if __name__ == "__main__":
    unittest.main()
