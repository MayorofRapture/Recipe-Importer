"""Shared, local-only helpers for the Streamlit pages."""
from __future__ import annotations

import os
import hashlib
import json
import uuid
from types import SimpleNamespace
from pathlib import Path
from typing import Any

import streamlit as st

from quick_edit import parse_ingredients, parse_instructions, to_quick_edit
from recipe_quality import RecipeCandidate
from draft_store import DraftStore
from review_store import ReviewStore
from ui_data import RecipeView, load_catalog
from ui_review_state import quick_edit_to_structured, row_ids_for
from ui_diagnostics import count


def paths() -> tuple[Path, Path | None]:
    from runtime_safety import FixtureModeError, fixture_mode, fixture_output_dir
    try:
        output = (fixture_output_dir() if fixture_mode()
                  else Path(os.environ.get("RECIPE_IMPORTER_OUTPUT_DIR", "output")).expanduser().resolve())
    except FixtureModeError as exc:
        st.error(str(exc), icon=":material/lock:")
        st.stop()
    source_value = os.environ.get("RECIPE_IMPORTER_SOURCE_DIR", "")
    return output, Path(source_value) if source_value else None


def catalog(*, read_only: bool = False) -> list[RecipeView]:
    count("catalog_loads")
    output, source_root = paths()
    try:
        return load_catalog(output, source_root, verify_sources=False, read_only=read_only)
    except Exception as exc:
        raise CatalogReadError(
            "Local records could not be read; history is preserved and mutations are blocked. "
            "Check the local review, import, and draft records before continuing."
        ) from exc


@st.cache_resource(show_spinner=False)
def _suggestion_service_for_output(output_path: str):
    """Share one durable suggestion worker per local output directory."""
    from suggestion_service import SuggestionService
    return SuggestionService(output_path)


def suggestion_service():
    """Return the configured service without creating provider work."""
    output, _ = paths()
    return _suggestion_service_for_output(str(output))


def enqueue_suggestion_request(view: RecipeView, *, operation_id: str):
    """Persist an explicitly authorized request, then hand it to a background worker."""
    from runtime_safety import FixtureModeError, fixture_mode
    if fixture_mode():
        raise FixtureModeError("Fixture mode disables live Gemini suggestion requests.")
    service = suggestion_service()
    request = service.request(view, suggestion_type="instructions", consent=True,
                              operation_id=operation_id)
    service.start_background(request["request_id"])
    return request


def retry_service():
    """Build the local retry boundary, using only synthetic extraction in fixture mode."""
    output, source_root = paths()
    from reconciliation_service import RetryService

    extractor = None
    from runtime_safety import fixture_mode
    if fixture_mode():
        from fixture_extraction import create_fixture_extractor
        extractor = create_fixture_extractor(output)
    return RetryService(output, source_root, extractor=extractor)


class CatalogReadError(RuntimeError):
    """A local catalog artifact failed to parse; mutation must stop safely."""


def buffer_for(view: RecipeView) -> dict[str, Any]:
    buffers = st.session_state.setdefault("review_buffers", {})
    existing = buffers.get(view.candidate_id)
    if existing is None:
        candidate = view.candidate
        revision_raw = getattr(view, "revision_raw_bulk_text", None)
        if view.revision and isinstance(revision_raw, dict):
            ingredients = str(revision_raw.get("ingredients", ""))
            instructions = str(revision_raw.get("instructions", ""))
            mode = str(getattr(view, "revision_mode", "structured"))
        elif view.draft is not None:
            ingredients = view.draft.raw_ingredients
            instructions = view.draft.raw_instructions
            mode = view.draft.mode
        else:
            ingredients, instructions = to_quick_edit(candidate.ingredients, candidate.instructions)
            mode = "structured"
        existing = {
            "raw_ingredients": ingredients,
            "raw_instructions": instructions,
            "mode": mode,
            "name": candidate.name,
            "yield_text": candidate.yield_text or "",
            "description": candidate.description or "",
            "recipe_notes": "\n".join(candidate.notes),
            "repair_proposal": (dict(getattr(view.draft, "repair_proposal", {}) or {})
                                if view.draft is not None else {}),
            "structured_ingredients": list(candidate.ingredients),
            "structured_instructions": list(candidate.instructions),
            "_revision": view.revision,
            "ingredient_ids": row_ids_for(
                "ingredients", len(candidate.ingredients), view.target_bindings,
                getattr(view, "revision_target_bindings", ())),
            "instruction_ids": row_ids_for(
                "instructions", len(candidate.instructions), view.target_bindings,
                getattr(view, "revision_target_bindings", ())),
        }
        buffers[view.candidate_id] = existing
        existing["_saved_signature"] = _buffer_signature(view, existing)
    existing.setdefault("name", view.candidate.name)
    existing.setdefault("yield_text", view.candidate.yield_text or "")
    existing.setdefault("description", view.candidate.description or "")
    return existing


def structured_widget_key(view: RecipeView, widget_prefix: str, row_id: str) -> str:
    """Give each saved revision fresh widget identity without changing target row IDs."""
    return f"{widget_prefix}:{view.candidate_id}:r{view.revision}:{row_id}"


def sync_buffer(view: RecipeView, buffer: dict[str, Any]) -> None:
    """Capture widget values before rendering a mode switch or action."""
    suggestion_edits = buffer.setdefault("_suggestion_edits", {})
    for suggestion in view.candidate.suggestions:
        key = f"suggestion_edit:{view.candidate_id}:{suggestion.suggestion_id}"
        if key in st.session_state:
            suggestion_edits[suggestion.suggestion_id] = st.session_state[key]
    for name in ("name", "yield_text", "description"):
        key = f"recipe_{name}:{view.candidate_id}"
        if key in st.session_state:
            buffer[name] = st.session_state[key]
    notes_key = f"recipe_notes:{view.candidate_id}"
    if notes_key in st.session_state:
        buffer["recipe_notes"] = st.session_state[notes_key]
    for section in ("ingredients", "instructions"):
        bulk_key = f"bulk_{section}:{view.candidate_id}"
        if bulk_key in st.session_state:
            buffer[f"raw_{section}"] = st.session_state[bulk_key]
    for name, key in (
        ("raw_ingredients", f"quick_ingredients:{view.candidate_id}"),
        ("raw_instructions", f"quick_instructions:{view.candidate_id}"),
    ):
        if key in st.session_state:
            buffer[name] = st.session_state[key]
    for field, values_key, ids_key, widget_prefix in (
        ("ingredients", "structured_ingredients", "ingredient_ids", "structured_i"),
        ("instructions", "structured_instructions", "instruction_ids", "structured_s"),
    ):
        values = list(buffer.get(values_key, []))
        row_ids = list(buffer.get(ids_key, []))
        changed = False
        for index, row_id in enumerate(row_ids[:len(values)]):
            key = structured_widget_key(view, widget_prefix, row_id)
            if key in st.session_state:
                value = st.session_state[key]
                changed = changed or value != values[index]
                values[index] = value
        buffer[values_key] = values
        if changed:
            separator = "\n" if field == "ingredients" else "\n\n"
            buffer[f"raw_{field}"] = separator.join(str(item) for item in values if str(item).strip())


def switch_buffer_mode(buffer: dict[str, Any], mode: str) -> None:
    """Convert the in-memory buffer before changing the visible editor."""
    current = str(buffer.get("mode", "structured"))
    if current == mode:
        return
    if mode == "structured":
        structured = quick_edit_to_structured(
            str(buffer.get("raw_ingredients", "")), str(buffer.get("raw_instructions", "")),
        )
        buffer["structured_ingredients"] = list(structured.ingredients)
        buffer["structured_instructions"] = list(structured.instructions)
    # Raw text is an independent draft. Switching away from Quick Edit must
    # not normalize or overwrite text that has not been explicitly applied.
    buffer["mode"] = mode


def candidate_from_buffer(view: RecipeView, buffer: dict[str, Any]) -> tuple[RecipeCandidate, list[str]]:
    candidate = view.candidate.model_copy(deep=True)
    candidate.name = str(buffer.get("name", candidate.name)).strip()
    candidate.yield_text = str(buffer.get("yield_text", candidate.yield_text or "")).strip() or None
    candidate.description = str(buffer.get("description", candidate.description or "")).strip() or None
    candidate.notes = [item.strip() for item in str(buffer.get("recipe_notes", "")).splitlines()
                       if item.strip()]
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
    recipe = candidate.model_dump(mode="json")
    revision = int(buffer.get("_revision", view.revision))
    if revision > 0 and not buffer_is_dirty(view, buffer):
        current = review_store().project(
            view.candidate_id, view.source_hash, view.extraction_id, view.candidate,
        )
        if current.revision != revision:
            from review_store import ReviewConflict
            raise ReviewConflict(f"revision changed: expected {revision}, current {current.revision}")
        save_resume_pointer(view, buffer)
        return SimpleNamespace(revision=revision, event_id=""), warnings
    draft_evidence = {
        "recipe": recipe,
        "raw_bulk_text": {"ingredients": str(buffer.get("raw_ingredients", "")),
                          "instructions": str(buffer.get("raw_instructions", ""))},
        "mode": str(buffer.get("mode", "quick")), "warnings": warnings,
    }
    target_bindings = _revision_bindings(view, buffer, candidate)
    draft_evidence["target_bindings"] = target_bindings
    operation_material = json.dumps(draft_evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    operation_id = "ui-save:" + hashlib.sha256(
        f"{view.candidate_id}:{view.extraction_id}:{revision}:{operation_material}".encode("utf-8")
    ).hexdigest()
    count("durable_writes")
    event = review_store().save_revision(
        candidate_id=view.candidate_id, source_hash=view.source_hash,
        extraction_id=view.extraction_id, expected_parent=revision,
        operation_id=operation_id, recipe=recipe,
        target_bindings=target_bindings,
        raw_bulk_text=draft_evidence["raw_bulk_text"], mode=draft_evidence["mode"],
        warnings=warnings,
    )
    buffer["_revision"] = int(event["revision"])
    buffer["_saved_signature"] = _buffer_signature(view, buffer)
    save_resume_pointer(view, buffer)
    return SimpleNamespace(revision=event["revision"], event_id=event["event_id"]), warnings


def save_repair_proposal(view: RecipeView, buffer: dict[str, Any], repair_proposal: dict[str, Any]) -> None:
    """Persist an unapproved proposal beside the current durable editor draft."""
    candidate, warnings = candidate_from_buffer(view, buffer)
    output, _ = paths()
    DraftStore(output / "ui_drafts.jsonl").save(
        view.candidate_id, view.source_hash, view.source_path,
        candidate.model_dump(mode="json"), str(buffer.get("raw_ingredients", "")),
        str(buffer.get("raw_instructions", "")), str(buffer.get("mode", "structured")),
        warnings, repair_proposal=repair_proposal,
    )
    buffer["repair_proposal"] = repair_proposal


def buffer_is_dirty(view: RecipeView, buffer: dict[str, Any]) -> bool:
    return buffer.get("_saved_signature") != _buffer_signature(view, buffer)


def _buffer_signature(view: RecipeView, buffer: dict[str, Any]) -> str:
    candidate, _warnings = candidate_from_buffer(view, buffer)
    material = {
        # Compare only fields the editor can change. Authority-derived flags,
        # issue dispositions, and diagnostic metadata may change after approval
        # without representing new user input that needs another revision.
        "recipe": {
            "name": candidate.name,
            "ingredients": candidate.ingredients,
            "instructions": candidate.instructions,
            "yield_text": candidate.yield_text,
            "description": candidate.description,
            "notes": candidate.notes,
        },
        "raw_bulk_text": {"ingredients": str(buffer.get("raw_ingredients", "")),
                          "instructions": str(buffer.get("raw_instructions", ""))},
        "mode": str(buffer.get("mode", "quick")),
    }
    return hashlib.sha256(json.dumps(material, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def save_resume_pointer(view: RecipeView, buffer: dict[str, Any] | None = None) -> None:
    """Persist the last confirmed review location without changing review authority."""
    output, _ = paths()
    output.mkdir(parents=True, exist_ok=True)
    state = st.session_state
    pointer = {
        "candidate_id": view.candidate_id,
        "source_hash": view.source_hash,
        "queue": state.get("review_queue", "Needs action"),
        "search": state.get("review_search", ""),
        "batch": state.get("review_batch", "All batches"),
        "status": state.get("review_status", "Any status"),
        "source_page": state.get(f"page:{view.candidate_id}", 1),
        "revision": int((buffer or {}).get("_revision", view.revision)),
    }
    target = output / "review_resume.json"
    try:
        if json.loads(target.read_text(encoding="utf-8")) == pointer:
            return
    except (OSError, json.JSONDecodeError):
        pass
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(pointer, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    temporary.replace(target)


def load_resume_pointer() -> dict[str, Any]:
    output, _ = paths()
    try:
        value = json.loads((output / "review_resume.json").read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def refresh_buffer_after_decision(candidate_id: str) -> None:
    """Refresh editor fields from the newly projected decision before widgets render."""
    view = next((item for item in catalog() if item.candidate_id == candidate_id), None)
    if view is None:
        return
    buffer = st.session_state.setdefault("review_buffers", {}).get(candidate_id)
    if buffer is None:
        return
    if buffer_is_dirty(view, buffer):
        if int(buffer.get("_revision", view.revision)) != view.revision:
            st.session_state.review_transition_error = (
                "A newer saved revision is available. Your unsaved edits remain selected; reload and compare before saving."
            )
        return
    buffer["mode"] = "structured"
    for field in ("name", "yield_text", "description"):
        value = getattr(view.candidate, field) or ""
        buffer[field] = value
        widget_key = f"recipe_{field}:{candidate_id}"
        if widget_key in st.session_state:
            st.session_state[widget_key] = value
    buffer["structured_ingredients"] = list(view.candidate.ingredients)
    buffer["structured_instructions"] = list(view.candidate.instructions)
    buffer["ingredient_ids"] = row_ids_for(
        "ingredients", len(view.candidate.ingredients), view.target_bindings,
        getattr(view, "revision_target_bindings", ()))
    buffer["instruction_ids"] = row_ids_for(
        "instructions", len(view.candidate.instructions), view.target_bindings,
        getattr(view, "revision_target_bindings", ()))
    buffer["_revision"] = view.revision
    buffer["_saved_signature"] = _buffer_signature(view, buffer)
    # Structured row widget keys are revision-scoped. The new projected
    # revision therefore cannot rehydrate a prior browser-side widget value.
    st.session_state.pop(f"mode:{candidate_id}", None)
    save_resume_pointer(view, buffer)


def guard_page_navigation(next_page: str) -> bool:
    """Flush Review before the native app navigation leaves that page."""
    prior_page = str(st.session_state.get("_review_last_page", ""))
    st.session_state._review_last_page = next_page
    if "review" not in prior_page.casefold() or "review" in next_page.casefold():
        return True
    candidate_id = st.session_state.get("review_active_selection", "")
    view = next((item for item in catalog() if item.candidate_id == candidate_id), None)
    if view is None:
        return True
    buffer = buffer_for(view)
    sync_buffer(view, buffer)
    if not buffer_is_dirty(view, buffer):
        save_resume_pointer(view, buffer)
        return True
    try:
        save_draft(view, buffer)
        return True
    except Exception as exc:
        st.session_state._review_last_page = prior_page
        st.session_state.review_transition_error = (
            f"Draft was not saved. Your edits remain selected in Review; retry the save before leaving. {exc}"
        )
        return False


def _revision_bindings(view: RecipeView, buffer: dict[str, Any], candidate: RecipeCandidate) -> list[dict]:
    bindings: list[dict] = []
    for field, ids in (("ingredients", buffer.get("ingredient_ids", [])),
                       ("instructions", buffer.get("instruction_ids", []))):
        originals: dict[str, list[str]] = {}
        revision_rows = [item for item in getattr(view, "revision_target_bindings", ())
                         if item.get("field") == field]
        if revision_rows:
            prior_values = getattr(view.candidate, field)
            for item in revision_rows:
                position = item.get("position")
                if isinstance(position, int) and 0 <= position < len(prior_values):
                    originals.setdefault(str(prior_values[position]), []).append(item["target_id"])
        else:
            for item in view.target_bindings:
                if item.get("target_kind") == "row" and item.get("field") == field:
                    originals.setdefault(str(item.get("original_value", "")), []).append(item["target_id"])
        used: set[str] = set()
        for position, value in enumerate(getattr(candidate, field)):
            if buffer.get("mode") == "structured":
                target_id = ids[position] if position < len(ids) else str(uuid.uuid4())
            else:
                matching = originals.get(value, [])
                target_id = matching[0] if len(matching) == 1 and matching[0] not in used else str(uuid.uuid4())
            used.add(target_id)
            bindings.append({"target_id": target_id, "field": field, "position": position,
                             "value_sha256": hashlib.sha256(value.encode("utf-8")).hexdigest()})
    return bindings


def review_store() -> ReviewStore:
    output, _ = paths()
    return ReviewStore(output / "review_events.jsonl")
