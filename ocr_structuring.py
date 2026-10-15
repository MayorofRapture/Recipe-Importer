"""Local assembly and validation for OCR line-reference structure."""
from __future__ import annotations

import re
from typing import Iterable

from pydantic import BaseModel, Field

from recipe_quality import Diagnostic, ExtractionBatch, RecipeCandidate


class LineSpan(BaseModel):
    start: int
    end: int


class RecipeLineReferences(BaseModel):
    title: LineSpan | None = None
    ingredients: list[LineSpan] = Field(default_factory=list)
    instructions: list[LineSpan] = Field(default_factory=list)
    confidence: str = "low"
    uncertainties: list[str] = Field(default_factory=list)


class OcrLineReferences(BaseModel):
    recipes: list[RecipeLineReferences] = Field(default_factory=list)


class ReferenceValidationError(ValueError):
    """Gemini returned references that cannot be safely assembled."""


_INSTRUCTION_BREAK = re.compile(
    r"^\s*(?:(?:step\s*)?\d{1,3}(?:\s*[.):\-]|\s*$)|[-*•])(?:\s+|$)",
    re.IGNORECASE,
)


def numbered_lines(text: str) -> str:
    """Return the original OCR lines with stable one-based labels."""
    return "\n".join(f"{number}: {line}" for number, line in enumerate(text.splitlines(), start=1))


def segment_instruction_lines(indexed_lines: list[tuple[int, str]]) -> list[tuple[str, LineSpan]]:
    """Join soft-wrapped source lines while retaining each paragraph's line span."""
    paragraphs: list[tuple[str, LineSpan]] = []
    current: list[tuple[int, str]] = []

    def flush() -> None:
        if not current:
            return
        values: list[str] = []
        for _, raw in current:
            value = raw.strip()
            if not value:
                continue
            if values and values[-1].endswith("-") and value[:1].islower():
                values[-1] = values[-1] + value
            else:
                values.append(value)
        if values:
            paragraphs.append((
                " ".join(values),
                LineSpan(start=current[0][0], end=current[-1][0]),
            ))
        current.clear()

    for line_number, raw in indexed_lines:
        if not raw.strip():
            flush()
            continue
        if current and _INSTRUCTION_BREAK.match(raw):
            flush()
        current.append((line_number, raw))
    flush()
    return paragraphs


def _validate_span(span: LineSpan, line_count: int, label: str) -> range:
    if span.start < 1 or span.end < 1 or span.start > span.end:
        raise ReferenceValidationError(f"Invalid {label} range: {span.start}-{span.end}")
    if span.end > line_count:
        raise ReferenceValidationError(
            f"{label} range {span.start}-{span.end} exceeds {line_count} OCR lines")
    return range(span.start, span.end + 1)


def _validate_references(refs: OcrLineReferences, line_count: int) -> None:
    if not refs.recipes:
        raise ReferenceValidationError("Gemini returned no recipe references")
    used: dict[int, str] = {}
    for recipe_index, recipe in enumerate(refs.recipes, start=1):
        spans: Iterable[tuple[str, LineSpan]] = []
        if recipe.title is not None:
            spans = [("title", recipe.title)]
        spans = list(spans) + [("ingredients", span) for span in recipe.ingredients]
        spans += [("instructions", span) for span in recipe.instructions]
        for field, span in spans:
            for line in _validate_span(span, line_count, f"recipe {recipe_index} {field}"):
                previous = used.get(line)
                if previous:
                    raise ReferenceValidationError(
                        f"Overlapping OCR references at line {line}: {previous} and "
                        f"recipe {recipe_index} {field}")
                used[line] = f"recipe {recipe_index} {field}"


def _values(lines: list[str], spans: list[LineSpan]) -> list[str]:
    values: list[str] = []
    for span in spans:
        for line in lines[span.start - 1:span.end]:
            if line.strip():
                values.append(line.strip())
    return values


def _title(lines: list[str], span: LineSpan | None) -> str:
    if span is None:
        return ""
    return " ".join(line.strip() for line in lines[span.start - 1:span.end] if line.strip())


def _diagnostic(field: str, detail: str) -> Diagnostic:
    return Diagnostic(
        field_path=field,
        kind="not_extracted",
        detail=detail,
        suggested_action="Review the numbered OCR source and approve or edit before importing.",
    )


def assemble_line_references(text: str, refs: OcrLineReferences) -> ExtractionBatch:
    """Resolve validated one-based source spans into source-derived candidates."""
    lines = text.splitlines()
    _validate_references(refs, len(lines))
    candidates: list[RecipeCandidate] = []
    for recipe_index, recipe in enumerate(refs.recipes, start=1):
        name = _title(lines, recipe.title)
        ingredients = _values(lines, recipe.ingredients)
        instruction_parts: list[tuple[str, LineSpan]] = []
        for span in recipe.instructions:
            instruction_parts.extend(segment_instruction_lines([
                (line_number, lines[line_number - 1])
                for line_number in range(span.start, span.end + 1)
            ]))
        instructions = [value for value, _ in instruction_parts]
        findings: list[Diagnostic] = []
        if not name:
            findings.append(_diagnostic("name", "No title line was identified in the OCR source."))
        if not ingredients:
            findings.append(_diagnostic("ingredients", "No ingredient lines were identified in the OCR source."))
        if not instructions:
            findings.append(_diagnostic("instructions", "No instruction lines were identified in the OCR source."))
        ranges = []
        if recipe.title is not None:
            ranges.append({"field": "name", "start": recipe.title.start, "end": recipe.title.end})
        ranges.extend({"field": "ingredients", "start": span.start, "end": span.end}
                      for span in recipe.ingredients)
        ranges.extend({"field": "instructions", "start": span.start, "end": span.end}
                      for _, span in instruction_parts)
        candidates.append(RecipeCandidate(
            name=name,
            ingredients=ingredients,
            instructions=instructions,
            missing_information=findings,
            source_method="gemini_text",
            source_line_ranges=ranges,
            confidence=recipe.confidence,
            uncertainties=list(recipe.uncertainties),
            requires_review=True,
        ))
    return ExtractionBatch(
        recipes=candidates,
        general_warnings=["Structured from OCR line references; human review is required."],
    )
