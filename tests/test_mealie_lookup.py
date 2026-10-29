import unittest
from unittest.mock import Mock

from mealie_client import MealieClient


class MealieLookupTests(unittest.TestCase):
    def test_tag_creation_conflict_is_bounded_when_lookup_still_has_no_match(self):
        session = Mock()
        session.get.return_value.json.return_value = {"items": []}
        session.post.return_value.status_code = 409
        session.post.return_value.text = "conflict"
        with self.assertRaisesRegex(RuntimeError, "tag"):
            MealieClient("http://mealie.test", "unused", session).ensure_tag("Synthetic cookbook")
        self.assertEqual(session.post.call_count, 1)

    def test_lookup_returns_verified_recipe_detail(self):
        session = Mock()
        session.get.return_value.status_code = 200
        session.get.return_value.json.return_value = {"id": "synthetic-id", "slug": "rice", "name": "Rice"}
        self.assertEqual(MealieClient("http://mealie.test", "unused", session).get_recipe("rice")["slug"], "rice")

    def test_missing_recipe_is_not_proof_of_absence_for_another_slug(self):
        session = Mock()
        session.get.return_value.status_code = 404
        self.assertIsNone(MealieClient("http://mealie.test", "unused", session).get_recipe("rice"))

    def test_mismatched_lookup_is_rejected(self):
        session = Mock()
        session.get.return_value.status_code = 200
        session.get.return_value.json.return_value = {"id": "synthetic-id", "slug": "another-recipe"}
        with self.assertRaises(ValueError):
            MealieClient("http://mealie.test", "unused", session).get_recipe("rice")
