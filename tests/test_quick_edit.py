import unittest

from quick_edit import parse_ingredients, parse_instructions, to_quick_edit


class QuickEditTests(unittest.TestCase):
    def test_ingredients_keep_unicode_fractions_and_suspicious_lines(self):
        result = parse_ingredients("½ cup flour\n%\nNoy 0 9 2021\n\n1 tsp salt")

        self.assertEqual(result.items, ["½ cup flour", "%", "Noy 0 9 2021", "1 tsp salt"])
        self.assertEqual(
            {warning.field_path for warning in result.warnings},
            {"ingredients[1]", "ingredients[2]"},
        )
        self.assertIn("OCR", result.warnings[0].message)

    def test_instruction_blank_lines_make_steps_and_join_wrapped_lines(self):
        result = parse_instructions("Step 1\nMix ingredients.\n\nBake until done.\nCool.")

        self.assertEqual(result.items, ["Step 1 Mix ingredients.", "Bake until done. Cool."])
        self.assertEqual(result.warnings, [])

    def test_instruction_step_only_text_is_preserved_and_warned(self):
        result = parse_instructions("Step 1\n\nStep 2")

        self.assertEqual(result.items, ["Step 1", "Step 2"])
        self.assertEqual({warning.field_path for warning in result.warnings}, {"instructions[0]", "instructions[1]"})

    def test_structured_values_round_trip_to_quick_edit(self):
        ingredients, instructions = to_quick_edit(
            ["1 cup sugar", "½ tsp salt"],
            ["Mix well.", "Bake at 350°F."],
        )

        self.assertEqual(ingredients, "1 cup sugar\n½ tsp salt")
        self.assertEqual(instructions, "Mix well.\n\nBake at 350°F.")


if __name__ == "__main__":
    unittest.main()
