import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from recipe_quality import Diagnostic
from tests.ui_fixture_factory import write_ui_fixture
from app_pages.review import _apply_proposal, _registered_finding_binding


APP_PATH = Path(__file__).resolve().parents[1] / "streamlit_app.py"


class StreamlitStructuredReviewTests(unittest.TestCase):
    def test_accepted_optional_field_suggestion_updates_recipe_buffer(self):
        view = SimpleNamespace(target_bindings=[{
            "issue_id": "cranberry-yield", "source_target_id": "yield-field",
            "source_target_kind": "field", "field": "yield_text",
        }])
        suggestion = SimpleNamespace(
            suggestion_id="cranberry-yield", field_path="yield_text", generated=False,
        )
        buffer = {"yield_text": "", "_revision": 0}

        with patch("app_pages.review._resolve_suggestion") as resolve:
            _apply_proposal(view, suggestion, "accepted", "12 bars", buffer)

        self.assertEqual(buffer["yield_text"], "12 bars")
        resolve.assert_called_once_with(view, suggestion, "accepted", "12 bars", buffer,
                                        evidence_reference="")

    def _app(self, root: str) -> AppTest:
        fixture = write_ui_fixture(Path(root))
        self.fixture = fixture
        self.env = patch.dict(os.environ, {
            "RECIPE_IMPORTER_OUTPUT_DIR": str(fixture["output"]),
            "RECIPE_IMPORTER_SOURCE_DIR": str(Path(root, "sources")),
        }, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        return AppTest.from_file(str(APP_PATH)).run(timeout=10)

    def test_recipe_editor_has_direct_section_tabs_and_row_controls(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = self._app(tmp)
            self.assertEqual([item.label for item in app.get("tab")], ["Ingredients", "Instructions"])
            labels = [button.label for button in app.button]
            self.assertIn("Add ingredient", labels)
            candidate_id = app.session_state["review_selection"]
            app.session_state[f"recipe_sections:{candidate_id}"] = "Instructions"
            app.run(timeout=10)
            labels = [button.label for button in app.button]
            self.assertIn("Add step", labels)
            self.assertTrue(any(item.label == "Actions" for item in app.menu_button))

    def test_suggestion_is_inline_and_approval_gate_remains_separate(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = self._app(tmp)
            recipe_selector = next(item for item in app.selectbox if item.label == "Jump to recipe")
            recipe_selector.set_value("Cranberry & Caramel Date Bars").run(timeout=10)
            visible = "\n".join(item.value for item in [*app.markdown, *app.caption, *app.text] if hasattr(item, "value"))
            self.assertIn("AI-inferred completion", visible)
            self.assertIn("unverified", visible.casefold())
            self.assertTrue(any("Accept inferred suggestion" in button.label for button in app.button))
            self.assertTrue(any("Approve & next" in button.label for button in app.button))

            approve = next(button for button in app.button if "Approve & next" in button.label)
            self.assertTrue(approve.disabled)
            blockers = [item.value for item in app.warning if hasattr(item, "value")]
            self.assertTrue(any("critical source issue unresolved" in value.casefold()
                                for value in blockers), blockers)

    def test_source_diagnoses_are_summarized_beside_their_exact_recipe_fields(self):
        diagnostics = [
            Diagnostic(field_path="name", kind="missing_from_source", detail="The title is absent.",
                       suggested_action="Confirm a title from another source."),
            Diagnostic(field_path="ingredients[0]", kind="ambiguous", detail="The quantity is unclear.",
                       source_evidence="The left edge is cropped."),
            Diagnostic(field_path="ingredients[1]", kind="unreadable", detail="The amount cannot be read.",
                       source_evidence="Ink is smudged."),
            Diagnostic(field_path="instructions[0]", kind="not_extracted", detail="Visible text was missed.",
                       source_evidence="A direction appears below the ingredients."),
            Diagnostic(field_path="yield_text", kind="missing_from_source", severity="optional",
                       detail="The source does not state a yield.", suggested_action="Leave blank if unknown."),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            fixture = write_ui_fixture(Path(tmp), cranberry_diagnostics=diagnostics)
            with patch.dict(os.environ, {
                "RECIPE_IMPORTER_OUTPUT_DIR": str(fixture["output"]),
                "RECIPE_IMPORTER_SOURCE_DIR": str(Path(tmp, "sources")),
            }, clear=False):
                app = AppTest.from_file(str(APP_PATH)).run(timeout=10)
                app.session_state.review_show_optional_details = True
                app = app.run(timeout=10)
                app = next(item for item in app.selectbox if item.label == "Jump to recipe").set_value(
                    "Cranberry & Caramel Date Bars").run(timeout=10)
                candidate_id = app.session_state["review_selection"]
                app.session_state[f"recipe_sections:{candidate_id}"] = "Instructions"
                app = app.run(timeout=10)
                instructions_labels = "\n".join(
                    str(item.value) for item in [*app.markdown, *app.caption, *app.warning, *app.info])
                app.session_state[f"recipe_sections:{candidate_id}"] = "Ingredients"
                app = app.run(timeout=10)

        self.assertFalse(app.exception)
        labels = instructions_labels + "\n" + "\n".join(
            str(item.value) for item in [*app.markdown, *app.caption, *app.warning, *app.info])
        self.assertIn("Missing from source", labels)
        self.assertIn("Ambiguous", labels)
        self.assertIn("Unreadable", labels)
        self.assertIn("Visible but not extracted", labels)
        self.assertIn("Optional note", labels)
        self.assertIn("The left edge is cropped.", labels)
        self.assertIn("A direction appears below the ingredients.", labels)

    def test_pending_proposal_stays_separate_from_authoritative_editor_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = self._app(tmp)
            app = next(item for item in app.selectbox if item.label == "Jump to recipe").set_value(
                "Cranberry & Caramel Date Bars").run(timeout=10)

        ingredient = next(item for item in app.text_input if item.label == "Ingredient 1")
        visible = "\n".join(str(item.value) for item in [*app.markdown, *app.caption, *app.text])
        proposal_labels = [str(item.label) for item in app.expander]
        self.assertTrue(any("Proposal · 1 cup dates · AI-inferred · Unverified" in label
                            for label in proposal_labels), proposal_labels)
        self.assertIn("AI-inferred completion", visible)
        self.assertIn("unverified", visible.casefold())
        self.assertEqual(ingredient.value, "dates")
        self.assertNotEqual(ingredient.value, "1 cup dates")
        self.assertTrue(any(item.label == "Accept inferred suggestion" for item in app.button))

    def test_resolving_first_finding_keeps_second_finding_on_its_registered_row(self):
        diagnostics = [
            Diagnostic(field_path="ingredients[0]", kind="ambiguous", detail="First row issue."),
            Diagnostic(field_path="ingredients[1]", kind="unreadable", detail="Second row issue."),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            fixture = write_ui_fixture(Path(tmp), cranberry_diagnostics=diagnostics)
            with patch.dict(os.environ, {
                "RECIPE_IMPORTER_OUTPUT_DIR": str(fixture["output"]),
                "RECIPE_IMPORTER_SOURCE_DIR": str(Path(tmp, "sources")),
            }, clear=False):
                app = AppTest.from_file(str(APP_PATH)).run(timeout=10)
                app = next(item for item in app.selectbox if item.label == "Jump to recipe").set_value(
                    "Cranberry & Caramel Date Bars").run(timeout=10)
                first_row = next(item for item in app.text_input if item.label == "Ingredient 1")
                second_row = next(item for item in app.text_input if item.label == "Ingredient 2")
                self.assertEqual((first_row.value, second_row.value), ("dates", "cranberries"))
                first_checkbox = next(item for item in app.checkbox
                                      if item.label == "I checked the source and corrected this field")
                app = first_checkbox.set_value(True).run(timeout=10)
                correction = next(item for item in app.button if item.label == "Record source correction")
                app = correction.click().run(timeout=10)

        self.assertFalse(app.exception)
        captions = "\n".join(str(item.value) for item in app.caption)
        text = "\n".join(str(item.value) for item in [*app.text, *app.warning])
        self.assertNotIn("First row issue.", text)
        self.assertIn("Second row issue.", text)
        self.assertTrue(any("Unreadable in source" in caption for caption in captions.splitlines()))
        self.assertTrue(any("Issue details · Unreadable in source" == item.label for item in app.expander))

    def test_source_supported_proposal_stays_unverified_until_reviewed(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = write_ui_fixture(Path(tmp), cranberry_alternative_source_supported=True)
            with patch.dict(os.environ, {
                "RECIPE_IMPORTER_OUTPUT_DIR": str(fixture["output"]),
                "RECIPE_IMPORTER_SOURCE_DIR": str(Path(tmp, "sources")),
            }, clear=False):
                app = AppTest.from_file(str(APP_PATH)).run(timeout=10)
                app = next(item for item in app.selectbox if item.label == "Jump to recipe").set_value(
                    "Cranberry & Caramel Date Bars").run(timeout=10)
                proposal_labels = [str(item.label) for item in app.expander]
                self.assertTrue(any("Proposal · 1 cup cranberries · Source-supported · Unverified" in label
                                    for label in proposal_labels), proposal_labels)
                self.assertTrue(any(item.label == "I verified this correction against the source scan"
                                    for item in app.checkbox))
                use = next(item for item in app.button if item.label == "Use suggestion")
                self.assertTrue(use.disabled)
                verified = next(item for item in app.checkbox
                                 if item.label == "I verified this correction against the source scan")
                app = verified.set_value(True).run(timeout=10)
                use = next(item for item in app.button if item.label == "Use suggestion")
                self.assertFalse(use.disabled)
                app = use.click().run(timeout=10)

        self.assertFalse(app.exception)
        ingredient = next(item for item in app.text_input if item.label == "Ingredient 2")
        self.assertEqual(ingredient.value, "1 cup cranberries")
        self.assertIn("Proposal · 1 cup cranberries · Source-supported · Applied",
                      [str(item.label) for item in app.expander])

    def test_finding_binding_uses_immutable_issue_payload_and_fails_closed(self):
        finding = Diagnostic(field_path="ingredients[1]", kind="unreadable", detail="Second row issue.")
        original = finding.model_dump(mode="json")
        binding = {
            "issue_id": "finding:8:ingredients[1]", "target_id": "finding-target-2",
            "target_kind": "finding", "field_path_at_extraction": "ingredients[1]",
            "source_target_id": "row-2", "source_target_kind": "row",
            "original_value": finding.detail, "original_issue": original,
        }
        view = SimpleNamespace(target_bindings=[binding])
        self.assertIs(_registered_finding_binding(view, finding), binding)

        legacy = {key: value for key, value in binding.items() if key != "original_issue"}
        self.assertIs(_registered_finding_binding(SimpleNamespace(target_bindings=[legacy]), finding), legacy)
        self.assertIsNone(_registered_finding_binding(SimpleNamespace(target_bindings=[]), finding))
        self.assertIsNone(_registered_finding_binding(SimpleNamespace(target_bindings=[binding, dict(binding)]), finding))

    def test_unrelated_row_proposal_remains_actionable_after_first_issue_revision(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = self._app(tmp)
            recipe_selector = next(item for item in app.selectbox if item.label == "Jump to recipe")
            recipe_selector.set_value("Cranberry & Caramel Date Bars").run(timeout=10)
            reject = next(item for item in app.button if item.label == "Reject; keep issue open")
            reject.click().run(timeout=10)

        visible = "\n".join(item.value for item in [*app.caption, *app.text] if hasattr(item, "value"))
        self.assertFalse(any("original target is unavailable" in item.value.casefold()
                             for item in app.caption if hasattr(item, "value")))
        self.assertTrue(any(item.label == "Edit and use" for item in app.button), visible)


if __name__ == "__main__":
    unittest.main()
