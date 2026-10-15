import unittest

from recipe_quality import RecipeCandidate, Suggestion
from ui_data import RecipeView
from ui_review_state import (
    StructuredBuffer,
    add_structured_item,
    apply_structured_change,
    move_structured_item,
    quick_edit_to_structured,
    remove_structured_item,
    structured_from_candidate,
    structured_to_quick_edit,
)


def _view() -> RecipeView:
    candidate = RecipeCandidate(
        name="Cranberry Date Bars",
        ingredients=["dates", "cranberries"],
        instructions=["Mix.", "Bake."],
        suggestions=[Suggestion(
            suggestion_id="step-suggestion",
            field_path="instructions[1]",
            proposed_value="Bake until set.",
            rationale="The source is partially cropped.",
            uncertainty="The final words are not visible.",
        )],
    )
    return RecipeView(
        candidate_id="source:cranberry", source_hash="source", source_path="source.pdf",
        candidate=candidate, status="Needs attention",
    )


class StructuredReviewStateTests(unittest.TestCase):
    def test_restores_candidate_and_keeps_suggestion_target(self):
        buffer = structured_from_candidate(_view())

        self.assertEqual(buffer.ingredients, ("dates", "cranberries"))
        self.assertEqual(buffer.instructions, ("Mix.", "Bake."))
        self.assertEqual(buffer.suggestions[0].field_path, "instructions[1]")
        self.assertEqual(buffer.suggestions[0].index, 1)

    def test_quick_and_structured_forms_round_trip_blank_step_boundaries(self):
        original = StructuredBuffer(
            ingredients=("1 cup dates", "1/2 cup cranberries"),
            instructions=("Mix the fruit.", "Bake until set."),
        )

        ingredients, instructions = structured_to_quick_edit(original)
        restored = quick_edit_to_structured(ingredients, instructions)

        self.assertEqual(restored.ingredients, original.ingredients)
        self.assertEqual(restored.instructions, original.instructions)

    def test_edit_add_remove_and_move_are_bounds_safe_and_ordered(self):
        original = StructuredBuffer(ingredients=("one", "two"), instructions=("first", "second"))
        edited = apply_structured_change(original, "ingredients", 1, "TWO")
        added = add_structured_item(edited, "ingredients", 1, "between")
        moved = move_structured_item(added, "ingredients", 2, -1)
        removed = remove_structured_item(moved, "ingredients", 1)

        self.assertEqual(removed.ingredients, ("one", "between"))
        self.assertEqual(move_structured_item(original, "instructions", 0, -1), original)
        self.assertEqual(remove_structured_item(original, "instructions", 99), original)


if __name__ == "__main__":
    unittest.main()
