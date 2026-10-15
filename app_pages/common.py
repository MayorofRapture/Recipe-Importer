"""Shared, local-only helpers for the Streamlit pages."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import streamlit as st

from draft_store import DraftStore
from quick_edit import parse_ingredients, parse_instructions, to_quick_edit
from recipe_quality import RecipeCandidate
from review_store import ReviewStore
from ui_data import RecipeView, load_catalog


def paths() -> tuple[Path, Path | None]:
    output = Path(os.environ.get("RECIPE_IMPORTER_OUTPUT_DIR", "output"))
    source_value = os.environ.get("RECIPE_IMPORTER_SOURCE_DIR", "")
    return output, Path(source_value) if source_value else None


def catalog() -> list[RecipeView]:
    output, source_root = paths()
    return load_catalog(output, source_root)


def buffer_for(view: RecipeView) -> dict[str, Any]:
    buffers = st.session_state.setdefault("review_buffers", {})
    existing = buffers.get(view.candidate_id)
    if existing is None:
        candidate = view.candidate
        if view.draft is not None:
            ingredients = view.draft.raw_ingredients
            instructions = view.draft.raw_instructions
            mode = view.draft.mode
        else:
            ingredients, instructions = to_quick_edit(candidate.ingredients, candidate.instructions)
            mode = "quick"
        existing = {
            "raw_ingredients": ingredients,
            "raw_instructions": instructions,
            "mode": mode,
            "structured_ingredients": list(candidate.ingredients),
            "structured_instructions": list(candidate.instructions),
        }
        buffers[view.candidate_id] = existing
    return existing


def sync_buffer(view: RecipeView, buffer: dict[str, Any]) -> None:
    """Capture widget values before rendering a mode switch or action."""
    for name, key in (
        ("raw_ingredients", f"quick_ingredients:{view.candidate_id}"),
        ("raw_instructions", f"quick_instructions:{view.candidate_id}"),
    ):
        if key in st.session_state:
            buffer[name] = st.session_state[key]


def candidate_from_buffer(view: RecipeView, buffer: dict[str, Any]) -> tuple[RecipeCandidate, list[str]]:
    candidate = view.candidate.model_copy(deep=True)
    if buffer.get("mode") == "structured":
        candidate.ingredients = [str(item).strip() for item in buffer.get("structured_ingredients", []) if str(item).strip()]
        candidate.instructions = [str(item).strip() for item in buffer.get("structured_instructions", []) if str(item).strip()]
        warnings: list[str] = []
    else:
        ingredient_result = parse_ingredients(str(buffer.get("raw_ingredients", "")))
        instruction_result = parse_instructions(str(buffer.get("raw_instructions", "")))
        candidate.ingredients = ingredient_result.items
        candidate.instructions = instruction_result.items
        warnings = [
            f"{item.field_path}: {item.message}"
            for item in [*ingredient_result.warnings, *instruction_result.warnings]
        ]
    return candidate, list(dict.fromkeys(warnings))


def save_draft(view: RecipeView, buffer: dict[str, Any]):
    candidate, warnings = candidate_from_buffer(view, buffer)
    output, _ = paths()
    draft_path = output / "ui_drafts.jsonl"
    record = DraftStore(draft_path).save(
        candidate_id=view.candidate_id,
        source_hash=view.source_hash,
        source_path=view.source_path,
        recipe=candidate.model_dump(mode="json"),
        raw_ingredients=str(buffer.get("raw_ingredients", "")),
        raw_instructions=str(buffer.get("raw_instructions", "")),
        mode=str(buffer.get("mode", "quick")),
        warnings=warnings,
    )
    if DraftStore(draft_path).latest(view.candidate_id) != record:
        raise OSError("The saved draft could not be read back safely.")
    return record, warnings


def review_store() -> ReviewStore:
    output, _ = paths()
    return ReviewStore(output / "review_events.jsonl")
