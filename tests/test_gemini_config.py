"""Gemini credentials load consistently from project or process configuration."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gemini_config import gemini_api_key


class GeminiConfigTests(unittest.TestCase):
    def setUp(self):
        self._temporary_root = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary_root.cleanup)
        self.root = Path(self._temporary_root.name)

    def test_project_env_key_is_loaded_without_exposing_it(self):
        env_file = self.root / ".env"
        env_file.write_text("GEMINI_API_KEY=fixture-key\n", encoding="utf-8")
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(gemini_api_key(env_file), "fixture-key")

    def test_process_environment_takes_precedence_over_project_env(self):
        env_file = self.root / ".env"
        env_file.write_text("GEMINI_API_KEY=file-key\n", encoding="utf-8")
        with patch.dict(os.environ, {"GEMINI_API_KEY": "process-key"}, clear=True):
            self.assertEqual(gemini_api_key(env_file), "process-key")


if __name__ == "__main__":
    unittest.main()
