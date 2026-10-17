"""Gemini credentials load consistently from project or process configuration."""
import os
import shutil
import unittest
from pathlib import Path
from unittest.mock import patch
import uuid

from gemini_config import gemini_api_key


class GeminiConfigTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parent / ".test-tmp" / f"gemini-config-{uuid.uuid4().hex}"
        self.root.mkdir(parents=True)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

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
