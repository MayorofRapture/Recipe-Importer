import unittest

from recipe_quality import ExtractionBatch, RecipeCandidate, needs_review, schema_recipe
from import_recipes import _quality_issue, candidate_identity
from review_store import extraction_identity
from recipe_validation import propose_repair, validate_recipe


class RecipeValidationTests(unittest.TestCase):
    def test_detects_instruction_and_substitution_stored_as_ingredients(self):
        recipe = RecipeCandidate(
            name="RASPBERRY CREAM CENTER",
            ingredients=[
                "Combine 2 cups powdered sugar, 1/2 cup (1 stick) softened butter or margarine and 2 tablespoons raspberry-flavored liqueur* in small bowl; beat until smooth.",
                "*1/4 cup raspberry preserves and 1 teaspoon water may be substituted for the raspberry-flavored liqueur.",
            ],
            instructions=[],
            source_line_ranges=[
                {"field": "ingredients", "start": 12, "end": 12},
                {"field": "ingredients", "start": 13, "end": 13},
            ],
        )

        findings = validate_recipe(recipe)

        self.assertEqual([item.category for item in findings], [
            "instruction_in_ingredients", "substitution_in_ingredients",
        ])
        self.assertEqual(findings[0].source_references, [{"field": "ingredients", "start": 12, "end": 12}])
        self.assertTrue(all(item.heuristic and not item.source_confirmed for item in findings))
        self.assertEqual(len({item.diagnostic_id for item in findings}), len(findings))

    def test_does_not_flag_legitimate_verb_bearing_ingredients(self):
        recipe = RecipeCandidate(ingredients=[
            "1 cup chopped walnuts", "2 tablespoons melted butter", "1 package cake mix",
            "1 cup sifted flour", "1 tablespoon prepared mustard", "1 cup cooked rice", "2 eggs, beaten",
        ], instructions=["Bake until set."])

        self.assertEqual(validate_recipe(recipe), [])

    def test_reports_multiple_quantities_and_missing_directions_together(self):
        recipe = RecipeCandidate(
            ingredients=["Combine 1 cup flour and 2 eggs in a bowl."],
            instructions=[],
        )

        findings = validate_recipe(recipe)

        self.assertEqual(len(findings), 1)
        self.assertIn("instruction_in_ingredients", {item.category for item in findings})
        self.assertIn("instructions", findings[0].suggested_action.casefold())

    def test_stable_diagnostic_identity_ignores_unrelated_recipe_fields(self):
        original = RecipeCandidate(ingredients=["Stir 1 cup sugar until smooth."])
        updated = original.model_copy(update={"name": "Different title"})

        self.assertEqual(validate_recipe(original)[0].diagnostic_id,
                         validate_recipe(updated)[0].diagnostic_id)

    def test_proposal_separates_measured_ingredients_instructions_and_substitution_note(self):
        recipe = RecipeCandidate(
            ingredients=[
                "Combine 2 cups powdered sugar, 1/2 cup (1 stick) softened butter or margarine and 2 tablespoons raspberry-flavored liqueur* in small bowl; beat until smooth.",
                "*1/4 cup raspberry preserves and 1 teaspoon water may be substituted for the raspberry-flavored liqueur.",
            ],
            instructions=[],
        )

        proposal = propose_repair(recipe)

        self.assertEqual(proposal.ingredients, [
            "2 cups powdered sugar", "1/2 cup (1 stick) softened butter or margarine",
            "2 tablespoons raspberry-flavored liqueur*",
        ])
        self.assertEqual(proposal.instructions, [recipe.ingredients[0]])
        self.assertEqual(proposal.notes, [recipe.ingredients[1]])
        self.assertEqual(recipe.ingredients[0].startswith("Combine"), True)

    def test_unrecognized_repair_is_preserved_as_uncertain_and_source_is_unchanged(self):
        recipe = RecipeCandidate(ingredients=["Add some flour and cook it until done."])

        proposal = propose_repair(recipe)

        self.assertTrue(proposal.uncertainties)
        self.assertEqual(proposal.ingredients, recipe.ingredients)
        self.assertEqual(recipe.notes, [])

    def test_mixed_content_triggers_quality_escalation_and_human_review(self):
        recipe = RecipeCandidate(name="Cake", ingredients=["Combine 1 cup flour and 2 eggs in a bowl."],
                                 instructions=["Bake until set."])

        self.assertTrue(_quality_issue(ExtractionBatch(recipes=[recipe])))
        self.assertTrue(needs_review(recipe))

    def test_substitution_notes_are_kept_out_of_ingredient_payload(self):
        payload = schema_recipe(RecipeCandidate(
            name="Dessert", ingredients=["2 cups sugar"], notes=["Use jam instead of liqueur."],
        ))

        self.assertEqual(payload["recipeIngredient"], ["2 cups sugar"])
        self.assertIn("Recipe note: Use jam instead of liqueur.", payload["description"])

    def test_empty_notes_keep_legacy_candidate_and_extraction_identity(self):
        recipe = RecipeCandidate(name="Cake", ingredients=["1 cup flour"], instructions=["Bake."])
        old_payload = recipe.model_dump(mode="json")
        old_payload.pop("notes")

        self.assertEqual(candidate_identity("hash", recipe),
                         "hash:" + __import__("hashlib").sha256(
                             __import__("json").dumps(old_payload, ensure_ascii=False, sort_keys=True,
                                                       separators=(",", ":")).encode()).hexdigest()[:24])
        self.assertEqual(extraction_identity("hash", {"recipes": [old_payload]}),
                         extraction_identity("hash", {"recipes": [recipe.model_dump(mode="json")]}))


if __name__ == "__main__":
    unittest.main()
