"""Sanitized local fixtures for Streamlit/AppTest workflows."""
from __future__ import annotations

import json
from pathlib import Path

import fitz

from import_recipes import StateStore, candidate_identity, file_digest
from recipe_quality import Diagnostic, ExtractionBatch, RecipeCandidate, Suggestion
from review_store import ReviewStore, extraction_identity


def write_ui_fixture(
    root: Path, *, duplicate_titles: bool = False,
    duplicate_requires_review: bool = True,
) -> dict[str, Path | str]:
    output = root / "output"
    extracted = output / "extracted"
    source_root = root / "sources"
    extracted.mkdir(parents=True)
    source_root.mkdir(parents=True)
    (output / ".fixture-library").write_text("synthetic browser/test library", encoding="utf-8")
    source = source_root / "cookbook.pdf"
    _write_pdf(source)

    fried = RecipeCandidate(
        name="Fried Rice", ingredients=["2 cups rice", "1 egg"],
        instructions=["Cook rice.", "Fold in egg."],
    )
    caramel = RecipeCandidate(
        name="The Best Caramel Apples",
        ingredients=["apples", "caramel"] + ["%", "Noy 0 9 2021", "??"] * 5,
        instructions=["Step 1", "Mix ingredients.", "Step 2", "Bake."],
        requires_review=True,
    )
    cranberry = RecipeCandidate(
        name="Cranberry & Caramel Date Bars",
        ingredients=["dates", "cranberries"],
        instructions=["Mix and bake."],
        missing_information=[Diagnostic(
            field_path="ingredients[0]",
            kind="ambiguous",
            severity="critical",
            detail="The quantity is unclear in the source.",
            source_evidence="The left edge of the quantity is cropped.",
            suggested_action="Compare the source before approving.",
        )],
        suggestions=[Suggestion(
            suggestion_id="cranberry-quantity",
            field_path="ingredients[0]",
            proposed_value="1 cup dates",
            rationale="Plausible but not confirmed by the source.",
            confidence="low",
            uncertainty="The quantity is cropped.",
        ), Suggestion(
            suggestion_id="cranberry-quantity-alternative",
            field_path="ingredients[1]",
            proposed_value="1 cup cranberries",
            rationale="A separate synthetic ingredient edit for review isolation.",
            confidence="low",
            uncertainty="The quantity is cropped.",
        ), Suggestion(
            suggestion_id="cranberry-yield",
            field_path="yield_text",
            proposed_value="12 bars",
            rationale="A plausible yield that is not printed clearly.",
            confidence="low",
            uncertainty="The source yield is absent.",
        )],
    )
    imported = RecipeCandidate(
        name="Imported Recipe", ingredients=["1 cup flour"], instructions=["Bake."],
    )
    recipes = [fried, caramel, cranberry, imported]
    if duplicate_titles:
        recipes.append(RecipeCandidate(
            name="Fried Rice", ingredients=["1 cup cold rice"],
            instructions=["Stir in a hot pan."], requires_review=duplicate_requires_review,
        ))
    batch = ExtractionBatch(recipes=recipes)
    digest = file_digest(source)
    (extracted / f"{digest}.json").write_text(
        json.dumps({"schema_version": 2, "source_hash": digest, "batch": batch.model_dump(mode="json")}, ensure_ascii=False),
        encoding="utf-8",
    )

    reviews = ReviewStore(output / "review_events.jsonl")
    for candidate in (caramel, cranberry):
        reviews.add_candidate(str(source), candidate_identity(digest, candidate), candidate)
    reviews.register_candidates(
        str(source), digest, extraction_identity(digest, batch.model_dump(mode="json")), recipes,
    )

    state = StateStore(output / "import_state.jsonl")
    state.add(
        f"{digest}:3", "IMPORTED", "imported-recipe", str(source), imported.name,
        identity=candidate_identity(digest, imported), tag_status="tagged",
    )
    return {"output": output, "source": source, "digest": digest}


def _write_pdf(path: Path) -> None:
    document = fitz.open()
    page = document.new_page(width=600, height=800)
    page.insert_text((72, 72), "Fried Rice", fontsize=18)
    page.insert_textbox((72, 105, 520, 270), "2 cups rice\n1 egg\n\nCook rice.\nFold in egg.", fontsize=12)
    page = document.new_page(width=600, height=800)
    page.insert_text((72, 72), "The Best Caramel Apples", fontsize=18)
    page.insert_textbox((72, 105, 520, 270), "apples\ncaramel\n\nMix ingredients.\nBake.", fontsize=12)
    page.insert_text((72, 350), "Cranberry & Caramel Date Bars", fontsize=18)
    page.insert_textbox((72, 382, 520, 525), "? cup dates\ncranberries\n\nMix and bake.", fontsize=12)
    page.insert_textbox((72, 640, 520, 740), "Imported Recipe\n1 cup flour\nBake.", fontsize=12)
    document.save(path)
    document.close()
