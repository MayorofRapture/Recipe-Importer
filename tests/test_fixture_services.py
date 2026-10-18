import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fixture_services import FixtureMealie
from recipe_quality import RecipeCandidate
from tests.ui_fixture_factory import write_ui_fixture
from ui_data import load_catalog


class FixtureServicesTests(unittest.TestCase):
    def test_review_fixture_proposals_bind_to_independent_recipe_targets(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = write_ui_fixture(Path(tmp))
            view = next(item for item in load_catalog(
                Path(fixture["output"]), Path(fixture["source"]).parent)
                if item.candidate.name == "Cranberry & Caramel Date Bars")
            suggestions = {item.suggestion_id: item for item in view.candidate.suggestions}
            bindings = {item["issue_id"]: item for item in view.target_bindings
                        if "issue_id" in item}

        self.assertEqual(suggestions["cranberry-quantity-alternative"].field_path, "ingredients[1]")
        self.assertNotEqual(bindings["cranberry-quantity"]["source_target_id"],
                            bindings["cranberry-quantity-alternative"]["source_target_id"])
        self.assertEqual(bindings["cranberry-quantity-alternative"]["field"], "ingredients")

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
