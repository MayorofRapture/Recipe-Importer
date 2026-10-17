"""Source-faithful recipe models and quality diagnostics."""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


DiagnosticKind = Literal["missing_from_source", "unreadable", "cropped", "ambiguous", "not_extracted"]
Severity = Literal["critical", "optional"]
ReviewStatus = Literal["pending", "approved", "edited", "rejected", "declined"]


class Diagnostic(BaseModel):
    field_path: str
    kind: DiagnosticKind
    severity: Severity = "critical"
    detail: str
    source_evidence: str = ""
    suggested_action: str = "Review the source before importing."


class Suggestion(BaseModel):
    suggestion_id: str
    field_path: str
    proposed_value: str
    rationale: str
    confidence: str = "low"
    uncertainty: str = ""
    diagnostic_id: str = ""
    review_status: ReviewStatus = "pending"
    generated: bool = False
    classification: str = ""
    request_id: str = ""
    target_id: str = ""
    insertion_location: dict = Field(default_factory=dict)
    dynamic_status: str = ""


class RecipeCandidate(BaseModel):
    name: str = ""
    ingredients: list[str] = Field(default_factory=list)
    instructions: list[str] = Field(default_factory=list)
    yield_text: Optional[str] = None
    description: Optional[str] = None
    warnings: list[str] = Field(default_factory=list)
    missing_information: list[Diagnostic] = Field(default_factory=list)
    suggestions: list[Suggestion] = Field(default_factory=list)
    source_method: str = ""
    source_line_ranges: list[dict] = Field(default_factory=list)
    confidence: str = ""
    uncertainties: list[str] = Field(default_factory=list)
    requires_review: bool = False


class ExtractionBatch(BaseModel):
    recipes: list[RecipeCandidate] = Field(default_factory=list)
    general_warnings: list[str] = Field(default_factory=list)


def is_complete(recipe: RecipeCandidate) -> bool:
    """Return true when the source-derived minimum recipe fields exist."""
    return bool(recipe.name.strip() and
                any(x.strip() for x in recipe.ingredients) and
                any(x.strip() for x in recipe.instructions))


def needs_review(recipe: RecipeCandidate) -> bool:
    """Critical findings or unresolved proposals require human review."""
    return recipe.requires_review or any(item.severity == "critical" for item in recipe.missing_information) or any(
        item.review_status == "pending" for item in recipe.suggestions
    )


def schema_recipe(recipe: RecipeCandidate) -> dict:
    """Build a Mealie JSON-LD payload from authoritative fields only."""
    data = {
        "@context": "https://schema.org",
        "@type": "Recipe",
        "name": recipe.name.strip(),
        "recipeIngredient": [x.strip() for x in recipe.ingredients if x.strip()],
        "recipeInstructions": [
            {"@type": "HowToStep", "text": x.strip()}
            for x in recipe.instructions if x.strip()
        ],
    }
    if recipe.yield_text:
        data["recipeYield"] = recipe.yield_text.strip()
    if recipe.description:
        data["description"] = recipe.description.strip()
    return data
