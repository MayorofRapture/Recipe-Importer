"""Pure structured-edit state helpers used by the Review page and tests."""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, replace
from typing import Literal

from quick_edit import parse_ingredients, parse_instructions, to_quick_edit
from recipe_quality import Suggestion
from ui_data import RecipeView


EditField = Literal["ingredients", "instructions"]


def row_ids_for(
    field: EditField, count: int, registration_bindings: list[dict] | tuple[dict, ...],
    revision_bindings: list[dict] | tuple[dict, ...] = (),
) -> list[str]:
    """Restore row identity from the saved revision, falling back to extraction rows."""
    ids: list[str | None] = [None] * count
    for item in revision_bindings:
        position = item.get("position")
        target_id = item.get("target_id")
        if (item.get("field") == field and isinstance(position, int)
                and 0 <= position < count and target_id and ids[position] is None):
            ids[position] = str(target_id)
    for item in registration_bindings:
        position = item.get("original_position")
        target_id = item.get("target_id")
        if (item.get("target_kind") == "row" and item.get("field") == field
                and isinstance(position, int) and 0 <= position < count
                and ids[position] is None and target_id):
            ids[position] = str(target_id)
    used = {item for item in ids if item is not None}
    for position, item in enumerate(ids):
        if item is None or ids.count(item) > 1:
            row_id = str(uuid.uuid4())
            while row_id in used:
                row_id = str(uuid.uuid4())
            ids[position] = row_id
            used.add(row_id)
    return [str(item) for item in ids]


@dataclass(frozen=True)
class InlineSuggestion:
    suggestion_id: str
    field_path: str
    proposed_value: str
    rationale: str
    uncertainty: str
    index: int | None


@dataclass(frozen=True)
class StructuredBuffer:
    ingredients: tuple[str, ...] = ()
    instructions: tuple[str, ...] = ()
    suggestions: tuple[InlineSuggestion, ...] = ()


def structured_from_candidate(view: RecipeView, buffer: dict | None = None) -> StructuredBuffer:
    candidate = view.candidate
    values = buffer or {}
    ingredients = tuple(str(item) for item in values.get("structured_ingredients", candidate.ingredients))
    instructions = tuple(str(item) for item in values.get("structured_instructions", candidate.instructions))
    suggestions = tuple(_inline_suggestion(item) for item in candidate.suggestions)
    return StructuredBuffer(ingredients=ingredients, instructions=instructions, suggestions=suggestions)


def structured_to_quick_edit(buffer: StructuredBuffer) -> tuple[str, str]:
    return to_quick_edit(list(buffer.ingredients), list(buffer.instructions))


def quick_edit_to_structured(ingredients: str, instructions: str) -> StructuredBuffer:
    ingredient_result = parse_ingredients(ingredients)
    instruction_result = parse_instructions(instructions)
    return StructuredBuffer(
        ingredients=tuple(ingredient_result.items),
        instructions=tuple(instruction_result.items),
    )


def apply_structured_change(
    buffer: StructuredBuffer, field: EditField, index: int, value: str,
) -> StructuredBuffer:
    values = list(_values(buffer, field))
    if index < 0 or index >= len(values):
        return buffer
    values[index] = value
    return _with_values(buffer, field, values)


def add_structured_item(
    buffer: StructuredBuffer, field: EditField, index: int, value: str = "",
) -> StructuredBuffer:
    values = list(_values(buffer, field))
    values.insert(max(0, min(index, len(values))), value)
    return _with_values(buffer, field, values)


def remove_structured_item(buffer: StructuredBuffer, field: EditField, index: int) -> StructuredBuffer:
    values = list(_values(buffer, field))
    if index < 0 or index >= len(values):
        return buffer
    values.pop(index)
    return _with_values(buffer, field, values)


def move_structured_item(
    buffer: StructuredBuffer, field: EditField, index: int, direction: int,
) -> StructuredBuffer:
    values = list(_values(buffer, field))
    target = index + (-1 if direction < 0 else 1)
    if index < 0 or index >= len(values) or target < 0 or target >= len(values):
        return buffer
    values[index], values[target] = values[target], values[index]
    return _with_values(buffer, field, values)


def _values(buffer: StructuredBuffer, field: EditField) -> tuple[str, ...]:
    return buffer.ingredients if field == "ingredients" else buffer.instructions


def _with_values(buffer: StructuredBuffer, field: EditField, values: list[str]) -> StructuredBuffer:
    return replace(buffer, **{field: tuple(values)})


def _inline_suggestion(suggestion: Suggestion) -> InlineSuggestion:
    match = re.fullmatch(r"(ingredients|instructions)\[(\d+)\]", suggestion.field_path)
    return InlineSuggestion(
        suggestion_id=suggestion.suggestion_id,
        field_path=suggestion.field_path,
        proposed_value=suggestion.proposed_value,
        rationale=suggestion.rationale,
        uncertainty=suggestion.uncertainty,
        index=int(match.group(2)) if match else None,
    )
