"""M1 Review workspace: compare source, correct safely, and record decisions."""
from __future__ import annotations

import streamlit as st

from app_pages.common import (
    buffer_for,
    candidate_from_buffer,
    catalog,
    review_store,
    save_draft,
    sync_buffer,
)
from ui_data import next_unresolved, previous_unresolved, render_source_page


def main() -> None:
    st.title("Review recipes")
    st.caption("Compare each recipe with its scan, make only the corrections you intend, then approve it.")
    views = catalog()
    if not views:
        st.info("No extracted recipes are available yet. Uploading and extraction will be added in a later milestone.")
        return

    with st.sidebar:
        st.subheader("Review queue")
        queue = st.selectbox("Show", ["Needs action", "All unresolved", "All records"], key="review_queue")
        search = st.text_input("Find a recipe", placeholder="Search by recipe name")
        batch = st.selectbox("Batch", ["All batches", *sorted({view.source_hash for view in views})], key="review_batch")
        counts = {
            "Needs attention": sum(view.status == "Needs attention" for view in views),
            "Ready to review": sum(view.status == "Ready to review" for view in views),
            "Ready to import": sum(view.status == "Ready to import" for view in views),
            "Imported": sum(view.status == "Imported" for view in views),
        }
        st.caption(" · ".join(f"{label}: {value}" for label, value in counts.items()))

    candidates = views if queue == "All records" else [view for view in views if not view.protected]
    if queue == "Needs action":
        candidates = [view for view in candidates if view.status in {"Needs attention", "Ready to review", "Import failed"}]
    if batch != "All batches":
        candidates = [view for view in candidates if view.source_hash == batch]
    if search.strip():
        candidates = [view for view in candidates if search.casefold() in view.candidate.name.casefold()]

    if not candidates:
        st.success("This queue is clear.")
        st.page_link("app_pages/import_history.py", label="Open Import & history")
        return

    option_ids = [view.candidate_id for view in candidates]
    selected_id = st.session_state.get("review_selection")
    if selected_id not in option_ids:
        selected_id = option_ids[0]
    selected_id = st.selectbox(
        "Recipe", option_ids, index=option_ids.index(selected_id),
        format_func=lambda value: next(view.candidate.name for view in candidates if view.candidate_id == value),
        key="review_selection",
    )
    view = next(view for view in candidates if view.candidate_id == selected_id)
    buffer = buffer_for(view)
    sync_buffer(view, buffer)

    st.divider()
    st.subheader(view.candidate.name or "Untitled recipe")
    st.caption(f"Status: {view.status} · Batch: {view.source_hash}")
    if view.protected:
        st.warning("This recipe is protected because its Mealie outcome needs reconciliation. Editing and approval are disabled here.")

    left, right = st.columns([1, 1.2], gap="large")
    with left:
        _render_source(view)
    with right:
        _render_editor(view, buffer)

    _render_findings(view)
    _render_suggestions(view)
    _render_actions(view, buffer)


def _render_source(view) -> None:
    st.markdown("#### Source scan")
    if not view.source_path:
        st.info("The original scan path is not available for this recipe.")
        return
    page_number = st.number_input("Source page", min_value=1, max_value=100, value=1, step=1, key=f"page:{view.candidate_id}")
    preview = render_source_page(view.source_path, int(page_number))
    if preview.image is not None:
        st.image(preview.image, caption=f"Page {preview.page_number} of {preview.page_count}", width="stretch")
    else:
        st.warning(preview.message or "Source preview unavailable.")


def _render_editor(view, buffer) -> None:
    st.markdown("#### Recipe details")
    if view.protected:
        st.text_area("Ingredients", "\n".join(view.candidate.ingredients), disabled=True, key=f"protected_i:{view.candidate_id}")
        st.text_area("Instructions", "\n\n".join(view.candidate.instructions), disabled=True, key=f"protected_s:{view.candidate_id}")
        return

    mode = st.radio(
        "Edit mode", ["Quick Edit", "Structured Edit"],
        index=0 if buffer.get("mode") != "structured" else 1,
        horizontal=True, key=f"mode:{view.candidate_id}",
    )
    buffer["mode"] = "structured" if mode == "Structured Edit" else "quick"
    if buffer["mode"] == "quick":
        ingredients = st.text_area("Ingredients (one item per line)", value=buffer["raw_ingredients"], height=180,
                                   key=f"quick_ingredients:{view.candidate_id}")
        instructions = st.text_area("Instructions (blank lines separate steps)", value=buffer["raw_instructions"], height=220,
                                    key=f"quick_instructions:{view.candidate_id}")
        buffer["raw_ingredients"] = ingredients
        buffer["raw_instructions"] = instructions
        candidate, warnings = candidate_from_buffer(view, buffer)
        st.caption(f"Preview: {len(candidate.ingredients)} ingredients · {len(candidate.instructions)} steps")
        _render_parse_warnings(warnings)
    else:
        _render_structured_editor(view, buffer)


def _render_structured_editor(view, buffer) -> None:
    buffer.setdefault("structured_ingredients", list(view.candidate.ingredients))
    buffer.setdefault("structured_instructions", list(view.candidate.instructions))
    st.markdown("**Ingredients**")
    for index, value in enumerate(list(buffer["structured_ingredients"])):
        cols = st.columns([1, 8, 1])
        with cols[0]:
            st.caption(str(index + 1))
        with cols[1]:
            buffer["structured_ingredients"][index] = st.text_input(
                "Ingredient", value=value, label_visibility="collapsed", key=f"structured_i:{view.candidate_id}:{index}")
        with cols[2]:
            if st.button("×", key=f"remove_i:{view.candidate_id}:{index}", help="Remove this ingredient"):
                buffer["structured_ingredients"].pop(index)
                st.rerun()
    if st.button("Add ingredient", key=f"add_i:{view.candidate_id}"):
        buffer["structured_ingredients"].append("")
        st.rerun()
    st.markdown("**Instructions**")
    for index, value in enumerate(list(buffer["structured_instructions"])):
        cols = st.columns([1, 8, 1])
        with cols[0]:
            st.caption(str(index + 1))
        with cols[1]:
            buffer["structured_instructions"][index] = st.text_area(
                "Step", value=value, label_visibility="collapsed", key=f"structured_s:{view.candidate_id}:{index}", height=80)
        with cols[2]:
            if st.button("×", key=f"remove_s:{view.candidate_id}:{index}", help="Remove this step"):
                buffer["structured_instructions"].pop(index)
                st.rerun()
    if st.button("Add step", key=f"add_s:{view.candidate_id}"):
        buffer["structured_instructions"].append("")
        st.rerun()


def _render_findings(view) -> None:
    if view.warnings:
        with st.expander(f"Source and OCR warnings ({len(view.warnings)})", expanded=True):
            for warning in view.warnings:
                st.warning(warning)


def _render_parse_warnings(warnings: list[str]) -> None:
    if warnings:
        with st.expander(f"Possible scan issues ({len(warnings)})", expanded=True):
            for warning in warnings:
                st.warning(warning)


def _render_suggestions(view) -> None:
    pending = view.pending_suggestions
    if not pending:
        return
    st.markdown("#### Unverified proposals")
    st.caption("These are suggestions, not source transcription. Approve or edit one only after checking the scan.")
    store = review_store()
    for suggestion in pending:
        with st.container(border=True):
            st.write(f"**{suggestion.field_path}**")
            st.write(f"Proposal: {suggestion.proposed_value}")
            st.caption(f"Why: {suggestion.rationale} · Uncertainty: {suggestion.uncertainty or 'Not stated'}")
            edited = st.text_input("Approved value", value=suggestion.proposed_value,
                                   key=f"suggestion:{view.candidate_id}:{suggestion.suggestion_id}")
            cols = st.columns(4)
            if cols[0].button("Approve", key=f"approve_s:{view.candidate_id}:{suggestion.suggestion_id}"):
                store.record(view.source_path, view.candidate_id, view.candidate, suggestion.suggestion_id, "approved")
                st.rerun()
            if cols[1].button("Save edit", key=f"edit_s:{view.candidate_id}:{suggestion.suggestion_id}"):
                store.record(view.source_path, view.candidate_id, view.candidate, suggestion.suggestion_id, "edited", edited)
                st.rerun()
            if cols[2].button("Reject", key=f"reject_s:{view.candidate_id}:{suggestion.suggestion_id}"):
                store.record(view.source_path, view.candidate_id, view.candidate, suggestion.suggestion_id, "rejected")
                st.rerun()
            if cols[3].button("Keep pending", key=f"pending_s:{view.candidate_id}:{suggestion.suggestion_id}"):
                st.info("Left pending.")


def _render_actions(view, buffer) -> None:
    if view.protected:
        return
    st.divider()
    st.markdown("#### Review actions")
    cols = st.columns(5)
    if cols[0].button("Save draft", key=f"save:{view.candidate_id}"):
        _save(view, buffer, advance=False)
    if cols[1].button("Save & Next", key=f"save_next:{view.candidate_id}"):
        _save(view, buffer, advance=True)
    if cols[2].button("Approve & Next", key=f"approve:{view.candidate_id}"):
        _approve(view, buffer)
    if cols[3].button("Previous", key=f"previous:{view.candidate_id}"):
        previous = previous_unresolved(catalog(), view.candidate_id)
        if previous:
            st.session_state.review_selection = previous.candidate_id
            st.rerun()
    if cols[4].button("Skip for now", key=f"skip:{view.candidate_id}"):
        following = next_unresolved(catalog(), view.candidate_id)
        st.session_state.review_selection = following.candidate_id if following else None
        st.rerun()


def _save(view, buffer, advance: bool) -> None:
    try:
        record, _ = save_draft(view, buffer)
    except Exception as exc:
        st.error(f"Draft was not saved. Your current text remains on screen: {exc}")
        return
    st.success(f"Draft saved (revision {record.revision}).")
    if advance:
        following = next_unresolved(catalog(), view.candidate_id)
        st.session_state.review_selection = following.candidate_id if following else None
        st.rerun()


def _approve(view, buffer) -> None:
    candidate, _ = candidate_from_buffer(view, buffer)
    if any(item.severity == "critical" for item in candidate.missing_information):
        st.error("Resolve the critical source warning before approving this recipe.")
        return
    if any(item.review_status == "pending" for item in candidate.suggestions):
        st.error("Resolve each unverified proposal before approving this recipe.")
        return
    try:
        save_draft(view, buffer)
        store = review_store()
        store.record_candidate_decision(view.source_path, view.candidate_id, "approved")
        if review_store().candidate_decision(view.candidate_id) is None:
            raise OSError("Approval was not read back from the review ledger.")
    except Exception as exc:
        st.error(f"Approval was not saved. Your current text remains on screen: {exc}")
        return
    following = next_unresolved(catalog(), view.candidate_id)
    st.session_state.review_selection = following.candidate_id if following else None
    st.rerun()


main()
