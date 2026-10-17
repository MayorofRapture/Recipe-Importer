import csv
import tempfile
import unittest
from pathlib import Path

from recipe_quality import (
    Diagnostic,
    ExtractionBatch,
    RecipeCandidate,
    Suggestion,
    is_complete,
    needs_review,
    schema_recipe,
)
from review_store import ReviewDecision, ReviewStore, apply_review_decision


class QualityReviewTests(unittest.TestCase):
    def test_legacy_extraction_json_loads_with_empty_quality_fields(self):
        batch = ExtractionBatch.model_validate({
            "recipes": [{
                "name": "Toast",
                "ingredients": ["1 slice bread"],
                "instructions": ["Toast the bread."],
            }],
            "general_warnings": [],
        })

        self.assertEqual(batch.recipes[0].missing_information, [])
        self.assertEqual(batch.recipes[0].suggestions, [])
        self.assertTrue(is_complete(batch.recipes[0]))

    def test_missing_source_data_is_reviewable_but_never_enters_payload(self):
        candidate = RecipeCandidate(
            name="Apple Pie",
            ingredients=["6 apples", "sugar"],
            instructions=["Bake until golden."],
            missing_information=[Diagnostic(
                field_path="ingredients[1]",
                kind="missing_from_source",
                severity="critical",
                detail="The scan names sugar but prints no quantity.",
                source_evidence="The ingredient line reads 'sugar'.",
                suggested_action="Review a proposed quantity before use.",
            )],
            suggestions=[Suggestion(
                suggestion_id="s-1",
                field_path="ingredients[1]",
                proposed_value="1/2 cup sugar",
                rationale="Plausible amount for this recipe size; not visible in the source.",
                confidence="low",
                uncertainty="The source provides no quantity.",
                diagnostic_id="ingredients[1]",
            )],
        )

        self.assertTrue(needs_review(candidate))
        self.assertEqual(schema_recipe(candidate)["recipeIngredient"], ["6 apples", "sugar"])

    def test_review_decision_keeps_proposal_and_applies_only_explicit_edit(self):
        candidate = RecipeCandidate(
            name="Apple Pie",
            ingredients=["6 apples", "sugar"],
            instructions=["Bake until golden."],
            suggestions=[Suggestion(
                suggestion_id="s-1",
                field_path="ingredients[1]",
                proposed_value="1/2 cup sugar",
                rationale="Plausible but unverified.",
                confidence="low",
                uncertainty="No quantity is printed.",
            )],
        )

        with tempfile.TemporaryDirectory() as tmp:
            store = ReviewStore(Path(tmp) / "review.jsonl")
            store.record("source.pdf", "source-hash:title", candidate, "s-1", "edited", "3/4 cup sugar")
            reviewed = apply_review_decision(candidate, store.decisions_for("source-hash:title"))

        self.assertEqual(reviewed.ingredients[1], "3/4 cup sugar")
        self.assertEqual(reviewed.suggestions[0].proposed_value, "1/2 cup sugar")
        self.assertEqual(reviewed.suggestions[0].review_status, "edited")

    def test_review_csv_is_actionable(self):
        candidate = RecipeCandidate(
            name="Toast",
            ingredients=["bread"],
            instructions=["Toast."],
            missing_information=[Diagnostic(
                field_path="ingredients[0]",
                kind="ambiguous",
                severity="critical",
                detail="Quantity may be cropped.",
                source_evidence="Left edge is cut off.",
                suggested_action="Inspect the original scan.",
            )],
        )
        with tempfile.TemporaryDirectory() as tmp:
            store = ReviewStore(Path(tmp) / "review.jsonl")
            store.add_candidate("scan.jpg", "hash:toast", candidate)
            output = Path(tmp) / "needs_review.csv"
            store.write_csv(output)
            with output.open(newline="", encoding="utf-8-sig") as fh:
                rows = list(csv.DictReader(fh))

        self.assertEqual(rows[0]["source"], "scan.jpg")
        self.assertEqual(rows[0]["diagnosis"], "ambiguous")
        self.assertEqual(rows[0]["review_status"], "pending")

    def test_review_csv_includes_current_semantic_ingredient_warning(self):
        candidate = RecipeCandidate(
            name="Raspberry Cream Center",
            ingredients=["Combine 2 cups sugar and 1/2 cup butter in a bowl."],
            instructions=[],
        )
        with tempfile.TemporaryDirectory() as tmp:
            store = ReviewStore(Path(tmp) / "review.jsonl")
            store.add_candidate("scan.jpg", "candidate-id", candidate)
            output = Path(tmp) / "needs_review.csv"
            store.write_csv(output)
            with output.open(newline="", encoding="utf-8-sig") as fh:
                rows = list(csv.DictReader(fh))

        self.assertEqual(rows[0]["diagnosis"], "instruction_in_ingredients")
        self.assertEqual(rows[0]["review_status"], "pending")
        self.assertIn("Combine 2 cups sugar", rows[0]["source_evidence"])

    def test_review_csv_includes_suggestion_id_for_jalapeno_suggestion(self):
        candidate = RecipeCandidate(
            name="Jalapeño Peppers",
            ingredients=["1 lb jalapeño peppers"],
            instructions=[],
            missing_information=[Diagnostic(
                field_path="instructions",
                kind="missing_from_source",
                severity="critical",
                detail="The source contains no instructions.",
            )],
            suggestions=[Suggestion(
                suggestion_id="sug_instructions_1",
                field_path="instructions",
                proposed_value="1. Preheat oven.\n2. Bake peppers.",
                rationale="Unverified completion.",
            )],
        )

        with tempfile.TemporaryDirectory() as tmp:
            store = ReviewStore(Path(tmp) / "review.jsonl")
            store.add_candidate("jalapeno.jpg", "hash:jalapeno", candidate)
            output = Path(tmp) / "needs_review.csv"
            store.write_csv(output)
            with output.open(newline="", encoding="utf-8-sig") as fh:
                rows = list(csv.DictReader(fh))

        self.assertEqual(rows[0]["suggestion_id"], "sug_instructions_1")

    def test_approved_full_recipe_lists_preserve_individual_entries(self):
        candidate = RecipeCandidate(
            name="Jalapeño Peppers",
            ingredients=["old ingredient"],
            instructions=[],
            suggestions=[
                Suggestion(
                    suggestion_id="sug-ingredients",
                    field_path="ingredients",
                    proposed_value="1 lb jalapeño peppers\n4 oz sharp cheese, grated",
                    rationale="Unverified ingredient list.",
                ),
                Suggestion(
                    suggestion_id="sug-instructions",
                    field_path="instructions",
                    proposed_value="1. Preheat oven.\n2. Bake peppers.",
                    rationale="Unverified instructions.",
                ),
            ],
        )

        reviewed = apply_review_decision(candidate, [
            ReviewDecision("sug-ingredients", "approved", None),
            ReviewDecision("sug-instructions", "edited", "1. Preheat oven.\n2. Bake peppers."),
        ])

        self.assertEqual(reviewed.ingredients, [
            "1 lb jalapeño peppers", "4 oz sharp cheese, grated",
        ])
        self.assertEqual(reviewed.instructions, ["Preheat oven.", "Bake peppers."])
        self.assertEqual(reviewed.suggestions[1].proposed_value,
                         "1. Preheat oven.\n2. Bake peppers.")


if __name__ == "__main__":
    unittest.main()
