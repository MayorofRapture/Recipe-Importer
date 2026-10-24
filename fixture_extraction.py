"""Explicitly synthetic extractor for marked local demonstration libraries."""
from __future__ import annotations

from pathlib import Path

from recipe_quality import ExtractionBatch, RecipeCandidate
from runtime_safety import fixture_mode


def create_fixture_extractor(output_dir: Path | str):
    output = Path(output_dir).expanduser().resolve()
    if not fixture_mode() or not (output / ".fixture-library").is_file():
        raise RuntimeError("Synthetic extraction requires fixture mode and a marked demonstration library")

    def extract(source: dict, mode: str, config: dict) -> ExtractionBatch:
        filename = str(source.get("original_filename", "uploaded file"))
        candidate = RecipeCandidate(
            name=f"Synthetic demo from {Path(filename).stem}",
            ingredients=["1 synthetic cup of flour"],
            instructions=["Mix the synthetic demonstration ingredients."],
            warnings=["Synthetic demonstration content only; no recipe text was read from this file."],
            source_method="fixture_demo",
            confidence="none",
            uncertainties=["Synthetic fixture output; source content was not inspected."],
            requires_review=True,
        )
        return ExtractionBatch(
            recipes=[candidate],
            general_warnings=["Synthetic demonstration result; no recipe content was extracted."],
        )

    return extract
