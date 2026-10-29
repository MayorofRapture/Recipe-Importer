from __future__ import annotations

import json
import unittest

from mealie_client import MealieClient
from recipe_quality import RecipeCandidate


class _Response:
    status_code = 201
    text = ""

    @staticmethod
    def json():
        return "synthetic-recipe-slug"


class _Session:
    def __init__(self):
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return _Response()


class MealieClientContractTests(unittest.TestCase):
    def test_v3_create_adapter_sends_empty_instructions_unchanged(self):
        session = _Session()
        client = MealieClient("https://mealie.invalid", "synthetic-token", session=session)
        recipe = RecipeCandidate(name="Synthetic seasoning", ingredients=["1 tsp oregano"],
                                 instructions=[])

        slug = client.create_recipe(recipe)

        self.assertEqual(slug, "synthetic-recipe-slug")
        self.assertEqual(len(session.calls), 1)
        url, request = session.calls[0]
        self.assertEqual(url, "https://mealie.invalid/api/recipes/create/html-or-json")
        payload = json.loads(request["json"]["data"])
        self.assertEqual(payload["recipeInstructions"], [])
        self.assertEqual(payload["recipeIngredient"], ["1 tsp oregano"])
        self.assertFalse(request["json"]["includeTags"])
        self.assertFalse(request["json"]["includeCategories"])


if __name__ == "__main__":
    unittest.main()
