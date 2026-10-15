"""Sanitized local fixtures for Streamlit/AppTest workflows."""
from __future__ import annotations

import json
from pathlib import Path

import fitz

from import_recipes import StateStore, candidate_identity
from recipe_quality import Diagnostic, ExtractionBatch, RecipeCandidate, Suggestion
from review_store import ReviewStore


def write_ui_fixture(root: Path) -> dict[str, Path | str]:
    output = root / "output"
    extracted = output / "extracted"
    source_root = root / "sources"
    extracted.mkdir(parents=True)
    source_root.mkdir(parents=True)
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
        name="Cranberry Date Bars",
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
        )],
    )
    imported = RecipeCandidate(
        name="Imported Recipe", ingredients=["1 cup flour"], instructions=["Bake."],
    )
    batch = ExtractionBatch(recipes=[fried, caramel, cranberry, imported])
    digest = "fixture-cookbook-hash"
    (extracted / f"{digest}.json").write_text(
        json.dumps({"schema_version": 2, "source_hash": digest, "batch": batch.model_dump(mode="json")}, ensure_ascii=False),
        encoding="utf-8",
    )

    reviews = ReviewStore(output / "review_events.jsonl")
    for candidate in (caramel, cranberry):
        reviews.add_candidate(str(source), candidate_identity(digest, candidate), candidate)

    state = StateStore(output / "import_state.jsonl")
    state.add(
        f"{digest}:3", "IMPORTED", "imported-recipe", str(source), imported.name,
        identity=candidate_identity(digest, imported), tag_status="tagged",
    )
    return {"output": output, "source": source, "digest": digest}


def _write_pdf(path: Path) -> None:
    document = fitz.open()
    for index in range(2):
        page = document.new_page(width=600, height=800)
        page.insert_text((72, 100), f"Recipe source page {index + 1}", fontsize=18)
        page.insert_text((72, 140), "Fried Rice / Caramel Apples / Cranberry Date Bars", fontsize=11)
    document.save(path)
    document.close()
