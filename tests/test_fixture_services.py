import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fixture_services import FixtureMealie
from recipe_quality import RecipeCandidate


class FixtureServicesTests(unittest.TestCase):
    def test_fake_service_requires_both_fixture_flag_and_marked_library(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"RECIPE_IMPORTER_FIXTURE_MODE": "0"}):
                with self.assertRaises(RuntimeError):
                    FixtureMealie(Path(tmp))
            with patch.dict(os.environ, {"RECIPE_IMPORTER_FIXTURE_MODE": "1"}):
                with self.assertRaises(RuntimeError):
                    FixtureMealie(Path(tmp))

    def test_fake_created_recipe_and_tags_survive_client_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".fixture-library").write_text("synthetic", encoding="utf-8")
            with patch.dict(os.environ, {"RECIPE_IMPORTER_FIXTURE_MODE": "1"}):
                first = FixtureMealie(root)
                slug = first.create_recipe(RecipeCandidate(name="Synthetic rice", ingredients=["1 cup rice"],
                                                          instructions=["Cook."]))
                tag = first.ensure_tag("Synthetic cookbook")
                first.add_tag(slug, tag)
                restored = FixtureMealie(root)
                self.assertEqual(restored.get_recipe(slug)["name"], "Synthetic rice")
                self.assertEqual(restored.add_tag(slug, tag), "already_present")
                self.assertIsNone(restored.get_recipe("missing"))
