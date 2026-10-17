"""M1 Review workspace: compare source, correct safely, and record decisions."""
from __future__ import annotations

import hashlib
import os
import uuid
from pathlib import Path
import streamlit as st

from app_pages.common import (
    CatalogReadError,
    buffer_for,
    buffer_is_dirty,
    candidate_from_buffer,
    catalog,
    load_resume_pointer,
    refresh_buffer_after_decision,
    retry_service,
    review_store,
    save_resume_pointer,
    save_draft,
    structured_widget_key,
    sync_buffer,
)
from ui_data import render_source_page, verify_selected_source
from recipe_quality import is_complete
from ui_diagnostics import bind_session, count, new_counters, runtime_marker



def _move_in_queue(candidates, direction: int) -> None:
    if not candidates:
        return
    ids = [view.candidate_id for view in candidates]
    current = st.session_state.get("review_selection")
    if current is None:
        if direction < 0:
            st.session_state.review_selection = ids[-1]
            st.rerun()
        return
    index = ids.index(current) if current in ids else 0
    target = index + direction
    if direction > 0 and target >= len(ids):
        st.session_state.review_selection = None
    else:
        st.session_state.review_selection = ids[max(0, min(len(ids) - 1, target))]
    st.rerun()


def _resolve_suggestion(view, suggestion, disposition: str, final_value: str | None, buffer,
                        evidence_reference: str = "") -> None:
    store = review_store()
    binding = next((item for item in view.target_bindings
                    if item.get("issue_id") == suggestion.suggestion_id), None)
    if binding is None:
        raise ValueError("The proposal has no unambiguous registered target.")
    saved, _ = save_draft(view, buffer)
    revision = saved.revision
    digest = hashlib.sha256((disposition + str(final_value or "") + evidence_reference).encode("utf-8")).hexdigest()[:20]
    store.resolve_issue(
        candidate_id=view.candidate_id, source_hash=view.source_hash,
        extraction_id=view.extraction_id, revision=revision,
        issue_id=suggestion.suggestion_id, target_id=binding["target_id"],
        disposition=disposition, final_value=final_value,
        operation_id=f"ui-issue:{view.candidate_id}:{revision}:{suggestion.suggestion_id}:{digest}",
        evidence_reference=evidence_reference,
    )
    st.session_state.review_refresh_candidate = view.candidate_id
    if disposition in {"accepted", "edited"}:
        for index, finding in enumerate(view.candidate.missing_information):
            if finding.field_path != suggestion.field_path or finding.severity != "critical":
                continue
            finding_binding = next((item for item in view.target_bindings
                                    if item.get("target_kind") == "finding"
                                    and item.get("field_path_at_extraction") == finding.field_path), None)
            if finding_binding:
                store.resolve_issue(
                    candidate_id=view.candidate_id, source_hash=view.source_hash,
                    extraction_id=view.extraction_id, revision=revision,
                    issue_id=finding_binding["issue_id"], target_id=finding_binding["target_id"],
                    disposition="human_completion", final_value=final_value,
                    operation_id=f"ui-finding:{view.candidate_id}:{revision}:{index}:{digest}",
                )
                st.session_state.review_refresh_candidate = view.candidate_id


def _apply_proposal(view, suggestion, disposition: str, final_value: str | None, buffer) -> None:
    if disposition in {"accepted", "edited"}:
        binding = next((item for item in view.target_bindings
                        if item.get("issue_id") == suggestion.suggestion_id), None)
        if binding is None:
            raise ValueError("The proposal has no registered target.")
        target_id = binding.get("source_target_id")
        target_kind = binding.get("source_target_kind")
        field = binding.get("field")
        if target_kind == "row" and field in {"ingredients", "instructions"}:
            ids_key = "ingredient_ids" if field == "ingredients" else "instruction_ids"
            values_key = "structured_ingredients" if field == "ingredients" else "structured_instructions"
            row_ids = buffer.get(ids_key, [])
            if not target_id or target_id not in row_ids:
                raise ValueError("The original recipe row is no longer present; this proposal cannot be applied by position.")
            buffer[values_key][row_ids.index(target_id)] = str(final_value or "")
            _update_raw_section(buffer, field)
        elif target_kind == "field" and field in {"ingredients", "instructions"}:
            from quick_edit import parse_ingredients, parse_instructions
            parsed = parse_ingredients(str(final_value or "")) if field == "ingredients" else parse_instructions(str(final_value or ""))
            buffer[f"structured_{field}"] = list(parsed.items)
            buffer["ingredient_ids" if field == "ingredients" else "instruction_ids"] = [
                str(uuid.uuid4()) for _ in parsed.items
            ]
            _update_raw_section(buffer, field)
        else:
            raise ValueError("The proposal has no exact ingredient or instruction target.")
    _resolve_suggestion(view, suggestion, disposition, final_value, buffer)


def _resolve_finding(view, buffer, finding, index: int) -> None:
    binding = next((item for item in view.target_bindings
                    if item.get("target_kind") == "finding"
                    and item.get("field_path_at_extraction") == finding.field_path), None)
    if binding is None:
        raise ValueError("The source issue has no unambiguous registered target.")
    saved, _ = save_draft(view, buffer)
    review_store().resolve_issue(
        candidate_id=view.candidate_id, source_hash=view.source_hash,
        extraction_id=view.extraction_id, revision=saved.revision,
        issue_id=binding["issue_id"], target_id=binding["target_id"],
        disposition="source_corrected", final_value=None,
        operation_id=f"ui-finding:{view.candidate_id}:{saved.revision}:{index}:source-corrected",
        evidence_reference="Operator confirmed and corrected the field against the source scan.",
    )
    st.session_state.review_refresh_candidate = view.candidate_id


def _run_issue_action(action) -> None:
    try:
        action()
    except Exception as exc:
        st.session_state.review_transition_error = (
            f"The review decision was not recorded. This recipe and its edits remain selected. Retry after resolving the save conflict. {exc}"
        )
        if st.session_state.get("review_refresh_candidate"):
            st.rerun()
        st.error(st.session_state.pop("review_transition_error"))
        return
    st.rerun()


def _apply_pending_refresh() -> None:
    candidate_id = st.session_state.pop("review_refresh_candidate", None)
    if candidate_id:
        refresh_buffer_after_decision(candidate_id)


def _approval_block_reason(view, buffer) -> str | None:
    if view.protected:
        return "Approval is unavailable while this recipe needs Mealie reconciliation."
    if view.identity_status != "registered":
        return "This recipe cannot be approved until its identity is reconciled with the source."
    if not view.source_available:
        return "Relink the original scan and verify it matches this extraction before approving."
    if view.legacy_draft_conflict:
        return "A newer legacy draft exists. Save the current review revision and approve it again."
    candidate, _ = candidate_from_buffer(view, buffer)
    if not is_complete(candidate):
        return "Add a recipe name, at least one ingredient, and at least one instruction before approving."
    if any(item.severity == "critical" for item in candidate.missing_information):
        return "Resolve the critical source warning before approving this recipe."
    if any(item.review_status == "pending" for item in candidate.suggestions):
        return "Resolve each unverified proposal before approving this recipe."
    return None


def _advance_queue(direction: int) -> None:
    if "review_selection" in st.session_state:
        st.session_state.review_pending_advance = direction
        st.rerun()
    ids = st.session_state.get("review_queue_ids", [])
    current = st.session_state.get("review_selection")
    if not ids:
        st.session_state.review_selection = None
        st.rerun()
        return
    if current is None:
        if direction < 0:
            st.session_state.review_selection = ids[-1]
    elif current in ids:
        target = ids.index(current) + direction
        if direction > 0 and target >= len(ids):
            st.session_state.review_selection = None
        else:
            st.session_state.review_selection = ids[max(0, min(len(ids) - 1, target))]
    else:
        st.session_state.review_selection = ids[0]
    st.rerun()


def _queue_advance_callback(direction: int) -> None:
    """Let the ensuing page run load one catalog and flush before changing selection."""
    st.session_state.review_pending_advance = direction


def _apply_pending_advance(views) -> None:
    direction = st.session_state.pop("review_pending_advance", None)
    if direction is None:
        return
    current_id = st.session_state.get("review_active_selection", st.session_state.get("review_selection"))
    if current_id and not _flush_candidate(current_id, views):
        st.session_state.review_selection = current_id
        return
    ids = st.session_state.get("review_queue_ids", [])
    current = st.session_state.get("review_selection")
    if not ids:
        st.session_state.review_selection = None
    elif current is None:
        if direction < 0:
            st.session_state.review_selection = ids[-1]
    else:
        index = ids.index(current) if current in ids else 0
        target = index + direction
        st.session_state.review_selection = None if direction > 0 and target >= len(ids) else ids[max(0, min(len(ids) - 1, target))]


def _apply_pending_queue_change(views) -> None:
    """Flush once, then accept a queued Jump/filter change on this page run."""
    pending_selection = st.session_state.pop("review_pending_selection", None)
    pending_filter = st.session_state.pop("review_pending_filter", None)
    if pending_selection is None and pending_filter is None:
        return

    current_id = st.session_state.get("review_active_selection", st.session_state.get("review_selection"))
    if current_id and not _flush_candidate(current_id, views):
        st.session_state.review_selection = current_id
        if pending_filter is not None:
            key, _requested, previous = pending_filter
            st.session_state[key] = previous
        return

    if pending_filter is not None:
        key, requested, _previous = pending_filter
        st.session_state[key] = requested
        st.session_state.review_filter_snapshot[key] = requested
    if pending_selection is not None:
        st.session_state.review_selection = pending_selection


def _save(view, buffer, advance: bool) -> None:
    try:
        record, _ = save_draft(view, buffer)
    except Exception as exc:
        st.error(f"Draft was not saved. Your current text remains on screen: {exc}")
        return
    st.success(f"Draft saved (revision {record.revision}).")
    st.session_state.review_active_selection = view.candidate_id
    if advance:
        _advance_queue(1)


def _approve(view, buffer) -> None:
    candidate, _ = candidate_from_buffer(view, buffer)
    if any(item.severity == "critical" for item in candidate.missing_information):
        st.error("Resolve the critical source warning before approving this recipe.")
        return
    if any(item.review_status == "pending" for item in candidate.suggestions):
        st.error("Resolve each unverified proposal before approving this recipe.")
        return
    saved = None
    try:
        saved, _ = save_draft(view, buffer)
        store = review_store()
        store.approve_revision(
            candidate_id=view.candidate_id, source_hash=view.source_hash,
            extraction_id=view.extraction_id, revision=saved.revision,
            operation_id=f"ui-approve:{view.candidate_id}:{view.extraction_id}:{saved.revision}",
            source_path=view.source_path,
        )
    except Exception as exc:
        if saved is not None:
            st.error(f"Draft saved at revision {saved.revision}; approval was not recorded. This recipe remains selected. Retry approval after checking the source. {exc}")
        else:
            st.error(f"Approval was not saved. Your current text remains on screen: {exc}")
        return
    _advance_queue(1)


def _restore_resume_state(views) -> None:
    if "review_selection" in st.session_state:
        return
    pointer = load_resume_pointer()
    ids = {view.candidate_id for view in views}
    selected = pointer.get("candidate_id")
    if selected in ids:
        st.session_state.review_selection = selected
        st.session_state.review_active_selection = selected
        for key, field in (("review_queue", "queue"), ("review_search", "search"),
                           ("review_batch", "batch"), ("review_status", "status")):
            if key not in st.session_state and field in pointer:
                st.session_state[key] = pointer[field]
        if "source_page" in pointer:
            st.session_state[f"page:{selected}"] = pointer["source_page"]


def _flush_candidate(candidate_id: str, views=None) -> bool:
    if views is None:
        views = catalog()
    view = next((item for item in views if item.candidate_id == candidate_id), None)
    if view is None or view.protected:
        return True
    buffer = buffer_for(view)
    sync_buffer(view, buffer)
    if not buffer_is_dirty(view, buffer):
        return True
    try:
        save_draft(view, buffer)
    except Exception as exc:
        st.session_state.review_transition_error = (
            f"Draft was not saved. Your edits and current recipe are still selected; retry the save. {exc}"
        )
        return False
    return True


def _selection_changed() -> None:
    prior = st.session_state.get("review_active_selection")
    requested = st.session_state.get("review_selection")
    if prior and prior != requested:
        st.session_state.review_pending_selection = requested
        st.session_state.review_selection = prior


def _filter_changed(key: str) -> None:
    snapshots = st.session_state.setdefault("review_filter_snapshot", {})
    previous = snapshots.get(key, st.session_state.get(key))
    requested = st.session_state.get(key)
    if requested != previous:
        st.session_state.review_pending_filter = (key, requested, previous)
        st.session_state[key] = previous


def main() -> None:
    diagnostics = (st.session_state.setdefault("_ui_diagnostics", new_counters())
                   if os.environ.get("RECIPE_IMPORTER_UI_DIAGNOSTICS") == "1" else None)
    bind_session(diagnostics)
    count("full_page_executions")
    _apply_pending_refresh()
    try:
        views = catalog()
    except CatalogReadError as exc:
        st.error(str(exc), icon=":material/lock:")
        st.stop()
    _apply_pending_advance(views)
    if not views:
        st.info("No recipes are waiting for review. Add a source from Uploads to get started.")
        return
    st.markdown(
        "<style>[data-testid='stMainBlockContainer'], "
        "div[data-testid='stAppViewContainer'] .main .block-container "
        "{padding-top: .25rem !important; padding-bottom: .5rem !important;} "
        "[data-testid='stMainBlockContainer'] > div[data-testid='stVerticalBlock'] "
        "{gap: .5rem !important;} [data-testid='stMainBlockContainer'] h1 "
        "{font-size: 1.25rem !important; line-height: 1.3 !important; margin: 0 !important;}</style>",
        unsafe_allow_html=True,
    )

    comparison_attempt_id = st.session_state.get("review_comparison_attempt_id")
    if comparison_attempt_id:
        if _render_reconciliation(retry_service(), str(comparison_attempt_id), views):
            return

    _restore_resume_state(views)
    for key, default in (("review_queue", "Needs action"), ("review_search", ""),
                         ("review_source", "All source files"), ("review_status", "Any status"),
                         ("review_page", 1)):
        st.session_state.setdefault(key, default)
    st.session_state.setdefault("review_filter_snapshot", {
        "review_queue": st.session_state.review_queue,
        "review_search": st.session_state.review_search,
        "review_source": st.session_state.review_source,
        "review_status": st.session_state.review_status,
    })
    _apply_pending_queue_change(views)
    st.session_state.setdefault("review_selection", views[0].candidate_id)
    if st.session_state.get("review_transition_error"):
        st.error(st.session_state.pop("review_transition_error"))
    queue_page = _queue_page(views)
    candidates = queue_page["candidates"]
    if not candidates:
        _render_queue(views, queue_page)
        st.info("No recipes match these filters. Open Review queue to adjust them.")
        return
    if "review_selection" in st.session_state and st.session_state.review_selection is None:
        _render_queue(views, queue_page)
        st.success("You’ve reached the end of this queue.")
        st.button("Previous recipe", key="queue_end_previous",
                  on_click=_queue_advance_callback, args=(-1,))
        return
    selected_id = st.session_state.get("review_selection")
    ids = [view.candidate_id for view in candidates]
    if selected_id not in ids:
        selected_id = ids[0]
        st.session_state.review_selection = selected_id
    st.session_state.review_queue_ids = ids
    view = verify_selected_source(next(v for v in candidates if v.candidate_id == selected_id))
    buffer = buffer_for(view)
    sync_buffer(view, buffer)
    st.session_state.review_active_selection = selected_id
    save_resume_pointer(view, buffer)
    position = ids.index(selected_id) + 1
    st.title("Review")
    st.caption(f"{view.status} · {position} of {len(ids)}")
    _render_queue(views, queue_page)
    if view.protected:
        st.warning("Read-only until this recipe’s Mealie outcome is reconciled.")
    workspace_view = _render_actions(view, buffer)
    if workspace_view == "Source":
        _render_source(view)
    elif workspace_view == "Recipe":
        _render_editor_fragment(view, buffer)
    else:
        source, editor = st.columns([0.9, 1.1], gap="medium")
        with source:
            _render_source(view)
        with editor:
            _render_editor_fragment(view, buffer)
    _render_findings(view, buffer)
    _render_retry_panel(view, views)


def _render_retry_panel(view, views) -> None:
    panel = st.expander("Try a new extraction", expanded=False, on_change="rerun")
    if not panel.open:
        return
    from reconciliation_service import ReconciliationError

    with panel:
        try:
            service = retry_service()
            plan = service.preview(view.source_hash)
        except Exception as exc:
            st.error(f"A retry plan is unavailable. Existing review data remains unchanged. {exc}")
            return
        st.write(plan["mode_notice"])
        st.caption(f"{len(plan['unresolved'])} unresolved candidate(s) · "
                   f"{len(plan['protected_siblings'])} protected sibling(s) retained")
        if plan["unresolved"]:
            with st.expander("Candidates eligible for another extraction"):
                for item in plan["unresolved"]:
                    st.write(f"{item['title']} · {item['status']}")
        if plan["protected_siblings"]:
            with st.expander("Imported or uncertain siblings · read only"):
                for item in plan["protected_siblings"]:
                    st.write(f"{item['title']} · {item['status']}")

        previous = plan.get("previous_attempt")
        if previous:
            st.caption(f"Previous attempt · {previous.get('job_status', previous.get('status', 'unknown'))} · "
                       f"{previous.get('candidate_count', 0)} candidate(s), "
                       f"{previous.get('failure_count', 0)} file(s) need attention")
            if previous.get("status") == "cancelled":
                st.info("The cancelled attempt is preserved. Start a new extraction only if you want another attempt.")
            elif previous.get("job_status") in {"completed", "completed_with_errors"}:
                if st.button("Compare retry results", key=f"compare_retry:{view.source_hash}"):
                    st.session_state.review_comparison_attempt_id = previous["attempt_id"]
                    st.rerun()
            elif previous.get("job_status") in {"queued", "waiting_for_worker", "running", "pausing"}:
                if st.button("Refresh retry status", key=f"refresh_retry:{view.source_hash}"):
                    st.rerun()
                if st.button("Cancel retry", key=f"cancel_retry:{view.source_hash}"):
                    try:
                        service.cancel(previous["attempt_id"])
                        st.info("Retry cancelled. Existing drafts and imports remain unchanged.")
                    except Exception as exc:
                        st.error(f"Retry could not be cancelled. Existing review data remains unchanged. {exc}")
                    st.rerun()

        if not plan["unresolved"]:
            st.info("There are no unresolved candidates in this source to re-extract.")
            return
        mode_label = st.selectbox(
            "Extraction method for this attempt",
            ["Local OCR", "Gemini extraction", "OCR-assisted review"],
            key=f"retry_mode:{view.source_hash}",
        )
        modes = {"Local OCR": "local_ocr", "Gemini extraction": "gemini",
                 "OCR-assisted review": "ocr_assisted"}
        cloud_consent = False
        if modes[mode_label] in {"gemini", "ocr_assisted"}:
            from runtime_safety import fixture_mode
            label = ("I understand this extraction uses synthetic demonstration output."
                     if fixture_mode() else
                     "I agree to send this source scan to Google for the selected extraction.")
            cloud_consent = st.checkbox(label, key=f"retry_consent:{view.source_hash}")
        if st.button("Start a new extraction", key=f"start_retry:{view.source_hash}",
                     disabled=(modes[mode_label] in {"gemini", "ocr_assisted"} and not cloud_consent)):
            try:
                attempt = service.start_attempt(view.source_hash, modes[mode_label], cloud_consent)
                st.session_state.review_comparison_attempt_id = attempt["attempt_id"]
                st.success("A separate retry attempt started. Current drafts and protected siblings are unchanged.")
                st.rerun()
            except (OSError, ValueError, RuntimeError, ReconciliationError) as exc:
                st.error(f"The new extraction could not start. Existing drafts and imports are unchanged. {exc}")


def _render_reconciliation(service, attempt_id: str, active_views) -> bool:
    from reconciliation_service import ReconciliationError

    attempt = service.get_attempt(attempt_id)
    if attempt is None:
        st.error("This retry attempt is unavailable. Existing drafts remain unchanged.")
        if st.button("Return to current draft", key="return_missing_retry"):
            st.session_state.pop("review_comparison_attempt_id", None)
            st.rerun()
        return True
    status = attempt.get("job_status", attempt.get("status", "unknown"))
    st.subheader("Compare re-extraction")
    st.caption(f"Attempt {attempt_id[:8]} · {status} · source "
               f"{Path(attempt.get('source_path', '')).name or 'unavailable'}")
    if status not in {"completed", "completed_with_errors"}:
        st.info("The new extraction is not ready to compare. Earlier extraction, drafts, and imports remain available.")
        if status not in {"failed", "cancelled"} and st.button("Cancel retry", key=f"cancel_compare:{attempt_id}"):
            try:
                service.cancel(attempt_id)
                st.success("Retry cancelled; no candidates were promoted.")
            except Exception as exc:
                st.error(f"Retry could not be cancelled. {exc}")
        if st.button("Refresh retry status", key=f"refresh_compare:{attempt_id}"):
            st.rerun()
        if st.button("Return to current draft", key=f"return_pending:{attempt_id}"):
            st.session_state.pop("review_comparison_attempt_id", None)
            st.rerun()
        return True
    try:
        comparison = service.compare(attempt_id)
    except Exception as exc:
        st.error(f"Retry results cannot be compared. Earlier extraction and draft remain inspectable. {exc}")
        if st.button("Return to current draft", key=f"return_invalid:{attempt_id}"):
            st.session_state.pop("review_comparison_attempt_id", None)
            st.rerun()
        return True

    return_col, note_col = st.columns([1, 4], vertical_alignment="center")
    with return_col:
        if st.button("Return to current draft", key=f"return_comparison:{attempt_id}"):
            st.session_state.pop("review_comparison_attempt_id", None)
            st.rerun()
    with note_col:
        if comparison.get("source_changed"):
            st.warning("The source bytes differ from the active extraction. Review every section against the current scan before mapping.")
        st.caption("No result is applied automatically. Imported and uncertain siblings stay read only.")

    active_by_id = {item.candidate_id: item for item in active_views}
    old_rows = comparison.get("old_candidates", [])
    new_rows = comparison.get("new_candidates", [])
    mapped_ids = st.session_state.setdefault(f"reconciliation_mapped:{attempt_id}", {})
    for index, new_row in enumerate(new_rows):
        new_id = str(new_row["candidate_id"])
        title = str(new_row.get("title") or new_row.get("candidate", {}).get("name") or "Untitled recipe")
        with st.container(border=True):
            st.markdown(f"**New extraction {index + 1} · {title}**")
            if new_row.get("match_status") == "ambiguous_title":
                st.warning("This title is duplicated or changed. Choose an existing recipe or defer; no match was inferred.")
            elif new_row.get("suggested_match_id"):
                st.info("An exact content match is suggested as a clue only. Select a target explicitly.")
            if new_row.get("identity_status") != "registered":
                st.warning("This candidate has an ambiguous identity and remains held until its registration is safe.")
                continue

            choice_options = ["Choose a disposition", "Defer this candidate", "Declare a new recipe"]
            target_labels: dict[str, str] = {}
            for old_index, old_row in enumerate(old_rows):
                if old_row.get("protected"):
                    continue
                target_id = str(old_row["candidate_id"])
                target_view = active_by_id.get(target_id)
                if target_view is None or target_view.protected:
                    continue
                label = f"Update existing recipe {old_index + 1} · {old_row.get('title', 'Untitled')}"
                target_labels[label] = target_id
                choice_options.append(label)
            choice_key = f"reconciliation_choice:{attempt_id}:{new_id}"
            choice = st.selectbox("Existing recipe to update", choice_options,
                                  key=choice_key, label_visibility="visible")
            target_id = target_labels.get(choice)
            selected_view = active_by_id.get(target_id) if target_id else None
            left, right = st.columns(2, gap="medium")
            with left:
                st.markdown("**Current draft / earlier extraction**")
                if selected_view is None:
                    st.caption("Choose a current recipe to see its comparison.")
                else:
                    _render_comparison_candidate(
                        selected_view.candidate.model_dump(mode="json"),
                        new_row.get("candidate", {}), is_new=False,
                    )
                    st.caption(f"Current status: {selected_view.status} · revision {selected_view.revision}")
            with right:
                st.markdown("**New extraction**")
                _render_comparison_candidate(
                    new_row.get("candidate", {}),
                    selected_view.candidate.model_dump(mode="json") if selected_view else {},
                    is_new=True,
                )
                st.caption(f"Identity: {new_row.get('identity_status', 'unknown')} · {new_row.get('match_status', 'unmatched')}")

            if new_id in mapped_ids:
                st.success(mapped_ids[new_id])
                continue
            evidence = st.text_input("Why this mapping is correct", key=f"reconciliation_evidence:{attempt_id}:{new_id}")
            if target_id:
                sections = st.multiselect(
                    "Sections to use from the new extraction",
                    ["name", "ingredients", "instructions", "yield_text", "description"],
                    format_func=lambda value: value.replace("_", " ").title(),
                    key=f"reconciliation_sections:{attempt_id}:{new_id}",
                )
                if st.button("Apply selected sections", key=f"apply_mapping:{attempt_id}:{new_id}",
                             disabled=not sections or not evidence.strip()):
                    try:
                        result = service.apply_mapping(
                            attempt_id, new_id, target_id, sections, selected_view.revision,
                            f"ui-reconciliation:{attempt_id}:{new_id}:{uuid.uuid4()}", evidence,
                        )
                        mapped_ids[new_id] = (
                            f"Draft updated to revision {result['revision']}; approval is required again."
                        )
                        st.rerun()
                    except Exception as exc:
                        st.error(f"The mapping was not saved. The selected target and comparison remain available. {exc}")
            elif choice == "Declare a new recipe":
                sections = st.multiselect(
                    "Sections to carry into a separately reviewed recipe",
                    ["name", "ingredients", "instructions", "yield_text", "description"],
                    format_func=lambda value: value.replace("_", " ").title(),
                    key=f"new_sections:{attempt_id}:{new_id}",
                )
                st.caption("This records a separate identity for later review; it does not create an approval or import.")
                if st.button("Record as a new recipe", key=f"declare_new:{attempt_id}:{new_id}",
                             disabled=not sections or not evidence.strip()):
                    try:
                        result = service.apply_mapping(
                            attempt_id, new_id, None, sections, 0,
                            f"ui-reconciliation-new:{attempt_id}:{new_id}:{uuid.uuid4()}", evidence,
                        )
                        mapped_ids[new_id] = "New recipe identity recorded separately; review and approval are still required."
                        st.rerun()
                    except Exception as exc:
                        st.error(f"The new identity was not recorded. This candidate remains held. {exc}")
            elif choice == "Defer this candidate":
                st.caption("Deferring records that this result was inspected without changing any draft.")
                if st.button("Save defer decision", key=f"defer:{attempt_id}:{new_id}",
                             disabled=not evidence.strip()):
                    try:
                        service.defer(attempt_id, new_id,
                                      f"ui-reconciliation-defer:{attempt_id}:{new_id}:{uuid.uuid4()}", evidence)
                        mapped_ids[new_id] = "Deferred; no active recipe content changed."
                        st.rerun()
                    except Exception as exc:
                        st.error(f"The defer decision was not saved. This candidate remains available. {exc}")
    return True


def _render_comparison_candidate(candidate: dict, counterpart: dict, *, is_new: bool) -> None:
    for field, label in (("name", "Title"), ("yield_text", "Yield"), ("description", "Description"),
                         ("ingredients", "Ingredients"), ("instructions", "Instructions")):
        value = candidate.get(field) or ([] if field in {"ingredients", "instructions"} else "")
        other = counterpart.get(field) or ([] if field in {"ingredients", "instructions"} else "")
        compared = bool(counterpart)
        changed = compared and value != other
        suffix = ("changed in retry" if is_new else "changed in current draft") if changed else (
            "unchanged" if compared else "comparison pending")
        st.markdown(f"**{label} · {suffix}**")
        if field in {"ingredients", "instructions"}:
            other_lines = [str(item) for item in other]
            lines = [str(item) for item in value]
            if changed and compared:
                added = [item for item in lines if item not in other_lines]
                removed = [item for item in other_lines if item not in lines]
                if is_new:
                    for item in removed:
                        st.write(f"Removed from retry: {item}")
                    for item in added:
                        st.write(f"Added in retry: {item}")
                else:
                    for item in removed:
                        st.write(f"Removed from current draft: {item}")
                    for item in added:
                        st.write(f"Added in current draft: {item}")
            for index, item in enumerate(lines, 1):
                st.write(f"{index}. {item}")
        elif value:
            st.write(value)


def _queue_page(views):
    mode = st.session_state.get("review_queue", "Needs action")
    candidates = views if mode == "All recipes" else [v for v in views if not v.protected]
    if mode == "Needs action":
        candidates = [v for v in candidates if v.status in {"Needs attention", "Ready to review", "Import failed"}]
    needle = st.session_state.get("review_search", "").strip().casefold()
    if needle:
        candidates = [v for v in candidates if needle in v.candidate.name.casefold()
                      or needle in v.source_display_name.casefold()]
    source = st.session_state.get("review_source", "All source files")
    if source != "All source files":
        candidates = [v for v in candidates if v.source_display_name == source]
    status = st.session_state.get("review_status", "Any status")
    if status != "Any status":
        candidates = [v for v in candidates if v.status == status]
    pages = max(1, (len(candidates) + 24) // 25)
    page = min(max(1, int(st.session_state.get("review_page", 1))), pages)
    st.session_state.review_page = page
    start, end = (page - 1) * 25, min(page * 25, len(candidates))
    return {"candidates": candidates[start:end], "total": len(candidates),
            "pages": pages, "page": page, "start": start, "end": end}


def _render_queue(views, queue_page) -> None:
    queue_labels = ["Needs action", "All unresolved", "All recipes"]
    with st.popover("Queue"):
        cols = st.columns([1, 1.7, 1.4, 1.4], vertical_alignment="bottom")
        with cols[0]:
            st.selectbox("Show", queue_labels, key="review_queue", on_change=_filter_changed, args=("review_queue",))
        with cols[1]:
            st.text_input("Search title or source", placeholder="Recipe or filename", key="review_search",
                          on_change=_filter_changed, args=("review_search",))
        sources = sorted({v.source_display_name for v in views if v.source_path})
        with cols[2]:
            options = ["All source files", *sources]
            if st.session_state.review_source not in options:
                st.session_state.review_source = "All source files"
            st.selectbox("Source file", options, key="review_source", on_change=_filter_changed, args=("review_source",))
        with cols[3]:
            statuses = ["Any status", "Needs attention", "Ready to review", "Ready to import", "Imported", "Check Mealie", "Import failed"]
            st.selectbox("Status", statuses, key="review_status", on_change=_filter_changed, args=("review_status",))
        candidates = queue_page["candidates"]
        page, pages = queue_page["page"], queue_page["pages"]
        nav = st.columns([1, 3, 1], vertical_alignment="center")
        with nav[0]:
            if st.button("Previous page", key="review_previous_page", disabled=page == 1):
                st.session_state.review_page = page - 1
                st.rerun()
        with nav[1]:
            start = queue_page["start"] + 1 if candidates else 0
            st.caption(f"{start} to {queue_page['end']} of {queue_page['total']} - 25 per page")
        with nav[2]:
            if st.button("Next page", key="review_next_page", disabled=page >= pages):
                st.session_state.review_page = page + 1
                st.rerun()
        page_ids = [item.candidate_id for item in candidates]
        if page_ids and st.session_state.get("review_selection") is not None:
            if st.session_state.review_selection not in page_ids:
                st.session_state.review_selection = page_ids[0]
            st.selectbox(
                "Jump to recipe", page_ids,
                format_func=lambda cid: next(v.candidate.name for v in candidates if v.candidate_id == cid),
                key="review_selection", on_change=_selection_changed,
            )
        for item in candidates:
            st.caption(f"{item.candidate.name} - {item.status} - {item.source_display_name} - {item.batch_name}")

def _render_source(view) -> None:
    with st.container(border=True):
        st.markdown(f"**Source scan** · {view.source_display_name}")
        if not view.source_path:
            st.info("Original scan path is unavailable.")
            return
        if not view.source_verified or not view.source_available:
            st.warning("The current source file could not be verified against this extraction. Its bytes are not shown as source evidence.")
            return
        count("preview_renders")
        page_key = f"page:{view.candidate_id}"
        st.session_state.setdefault(page_key, int(load_resume_pointer().get("source_page", 1)))
        zoom_key = f"zoom:{view.candidate_id}"
        zoom_options = {"Fit": 1.0, "Inspect": 1.5, "More detail": 2.0}
        st.session_state.setdefault(zoom_key, "Fit")
        zoom = zoom_options[st.session_state[zoom_key]]
        preview = render_source_page(view.source_path, int(st.session_state.get(page_key, 1)), zoom=zoom)
        if preview.page_count:
            page = min(max(1, int(st.session_state.get(page_key, 1))), preview.page_count)
            st.session_state[page_key] = page
            controls = st.columns([1.3, 1], vertical_alignment="bottom")
            with controls[0]:
                st.selectbox("Page", range(1, preview.page_count + 1), key=page_key,
                             format_func=lambda value: f"{value} of {preview.page_count}")
            with controls[1]:
                st.selectbox("Source view", list(zoom_options), key=zoom_key)
            page = int(st.session_state[page_key])
            if page != preview.page_number:
                preview = render_source_page(view.source_path, page, zoom=zoom)
        if preview.image is not None:
            st.image(preview.image, width="stretch", alt=f"Recipe source page {preview.page_number}")
        else:
            st.warning(preview.message or "Source preview unavailable.")
        with st.expander("Source and extraction details"):
            st.caption(f"Collection: {view.batch_name}")
            st.caption(f"Method: {view.candidate.source_method or 'Not recorded'}")
            if view.candidate.confidence:
                st.caption(f"Reported confidence: {view.candidate.confidence}")
            for note in view.candidate.uncertainties:
                st.write(note)


@st.fragment
def _render_editor_fragment(view, buffer) -> None:
    diagnostics = (st.session_state.setdefault("_ui_diagnostics", new_counters())
                   if os.environ.get("RECIPE_IMPORTER_UI_DIAGNOSTICS") == "1" else None)
    bind_session(diagnostics)
    count("fragment_executions")
    # Tab and row widgets rerun this fragment only, so capture both sections
    # before Streamlit cleans up the inactive tab's widget state.
    sync_buffer(view, buffer)
    _render_editor(view, buffer)
    if diagnostics is not None:
        with st.expander("Performance counters"):
            st.caption(runtime_marker(st.context.url))
            st.json(dict(diagnostics))


def _render_editor(view, buffer) -> None:
    with st.container(border=True):
        st.markdown("**Recipe**")
        name_key = f"recipe_name:{view.candidate_id}"
        st.session_state.setdefault(name_key, str(buffer.get("name", view.candidate.name)))
        st.text_input("Recipe title", key=name_key, disabled=view.protected)
        if view.protected:
            st.text_area("Ingredients", "\n".join(view.candidate.ingredients), disabled=True, key=f"protected_i:{view.candidate_id}")
            st.text_area("Instructions", "\n\n".join(view.candidate.instructions), disabled=True, key=f"protected_s:{view.candidate_id}")
            return
        with st.expander("Optional details", expanded=st.session_state.get("review_show_optional_details", False)):
            st.text_input("Yield", value=str(buffer.get("yield_text", "")),
                          key=f"recipe_yield_text:{view.candidate_id}")
            st.text_area("Description", value=str(buffer.get("description", "")), height=84,
                         key=f"recipe_description:{view.candidate_id}")
        tab_key = f"recipe_sections:{view.candidate_id}"
        st.session_state.setdefault(tab_key, st.session_state.get(
            f"recipe_section_saved:{view.candidate_id}", "Ingredients"))
        tabs = st.tabs(["Ingredients", "Instructions"], key=tab_key,
                       on_change=_remember_recipe_section, args=(view.candidate_id,))
        buffer.setdefault("structured_ingredients", list(view.candidate.ingredients))
        buffer.setdefault("structured_instructions", list(view.candidate.instructions))
        buffer.setdefault("ingredient_ids", [str(uuid.uuid4()) for _ in buffer["structured_ingredients"]])
        buffer.setdefault("instruction_ids", [str(uuid.uuid4()) for _ in buffer["structured_instructions"]])
        if tabs[0].open:
            _render_structured_section(view, buffer, "ingredients")
        if tabs[1].open:
            _render_structured_section(view, buffer, "instructions")
        if st.button("Save draft", key=f"save:{view.candidate_id}", width="stretch"):
            _save(view, buffer, advance=False)
        if buffer_is_dirty(view, buffer):
            st.caption("Unsaved changes · use Save draft to keep these edits.")
        approval_reason = _approval_block_reason(view, buffer)
        if st.button("Approve & next", key=f"approve:{view.candidate_id}", disabled=bool(approval_reason),
                     type="primary", width="stretch"):
            _approve(view, buffer)
        if approval_reason:
            st.caption(approval_reason)


def _remember_recipe_section(candidate_id: str) -> None:
    st.session_state[f"recipe_section_saved:{candidate_id}"] = st.session_state.get(
        f"recipe_sections:{candidate_id}", "Ingredients")


def _update_raw_section(buffer, field: str) -> None:
    separator = "\n" if field == "ingredients" else "\n\n"
    values = buffer["structured_ingredients" if field == "ingredients" else "structured_instructions"]
    buffer[f"raw_{field}"] = separator.join(str(item) for item in values if str(item).strip())


def _render_bulk_editor(view, buffer, field: str) -> None:
    key = f"bulk_{field}:{view.candidate_id}"
    raw = buffer.get("raw_ingredients" if field == "ingredients" else "raw_instructions", "")
    if key not in st.session_state:
        buffer.setdefault(f"_bulk_original_{field}", raw)
        st.session_state[key] = raw
    st.caption("One ingredient per line." if field == "ingredients" else "Blank lines separate steps. Review the proposed boundaries before applying.")
    edited = st.text_area(f"Bulk {field}", key=key, height=280)
    parsed = [line.strip() for line in edited.splitlines() if line.strip()] if field == "ingredients" else [part.strip() for part in edited.split("\n\n") if part.strip()]
    prior = buffer.get("structured_ingredients" if field == "ingredients" else "structured_instructions", [])
    st.caption(f"Preview: {len(prior)} → {len(parsed)} {field} · {max(0, len(prior)-len(parsed))} removed")
    buttons = st.columns(2)
    if buttons[0].button(f"Apply {field}", key=f"apply_bulk_{field}:{view.candidate_id}"):
        _snapshot_undo(buffer)
        if field == "ingredients":
            buffer["structured_ingredients"] = parsed
            buffer["ingredient_ids"] = [str(uuid.uuid4()) for _ in parsed]
            buffer["raw_ingredients"] = edited
        else:
            buffer["structured_instructions"] = parsed
            buffer["instruction_ids"] = [str(uuid.uuid4()) for _ in parsed]
            buffer["raw_instructions"] = edited
        buffer.pop(f"_bulk_original_{field}", None)
        buffer[f"_bulk_open_{field}"] = False
        st.session_state.pop(key, None)
    if buttons[1].button("Cancel bulk edit", key=f"cancel_bulk_{field}:{view.candidate_id}"):
        buffer[f"raw_{field}"] = buffer.pop(f"_bulk_original_{field}", raw)
        buffer[f"_bulk_open_{field}"] = False
        st.session_state.pop(key, None)


def _queue_choice_changed() -> None:
    requested = st.session_state.get("review_queue_choice")
    current = st.session_state.get("review_active_selection")
    if current and current != requested and not _flush_candidate(current):
        st.session_state.review_queue_choice = current
        return
    if requested:
        st.session_state.review_selection = requested


def _render_actions(view, buffer) -> str:
    cols = st.columns([1, 1], vertical_alignment="center")
    with cols[0]:
        with st.popover("Options"):
            workspace_view = st.radio("Workspace view", ["Side by side", "Source", "Recipe"],
                                      key="review_workspace_view")
            st.button("Previous recipe", key=f"previous:{view.candidate_id}",
                      disabled=view.protected, width="stretch",
                      on_click=_queue_advance_callback, args=(-1,))
    with cols[1]:
        st.button("Skip for now", key=f"skip:{view.candidate_id}", disabled=view.protected,
                  width="stretch", on_click=_queue_advance_callback, args=(1,))
    state = st.session_state.get("review_save_state")
    if state:
        st.caption(state)
    return workspace_view


def _render_structured_section(view, buffer, field: str) -> None:
    labels = {"ingredients": ("Ingredients", "ingredient", "ingredient_ids", "structured_ingredients", "structured_i"),
              "instructions": ("Instructions", "step", "instruction_ids", "structured_instructions", "structured_s")}
    label, kind, ids_key, value_key, widget_prefix = labels[field]
    st.markdown(f"**{label}**")
    _render_suggestions(view, buffer, field)
    for index, value in enumerate(list(buffer[value_key])):
        row_id = buffer[ids_key][index]
        cols = st.columns([0.4, 6, 1.4], vertical_alignment="center")
        with cols[0]:
            st.caption(str(index + 1))
        with cols[1]:
            if field == "ingredients":
                buffer[value_key][index] = st.text_input(
                    f"Ingredient {index + 1}", value=value,
                    key=structured_widget_key(view, widget_prefix, row_id))
            else:
                buffer[value_key][index] = st.text_area(
                    f"Step {index + 1}", value=value,
                    key=structured_widget_key(view, widget_prefix, row_id), height=88)
        with cols[2]:
            action = st.menu_button("Actions", ["Insert below", "Move up", "Move down", "Merge with next", "Remove"],
                                    key=f"actions_{kind}:{view.candidate_id}:{row_id}",
                                    help=f"{label} {index + 1} actions",
                                    format_func=lambda value: value)
        if action:
            _mutate_row(view, buffer, value_key, ids_key, index, action)
            return
        _render_suggestions(view, buffer, field, row_id)
    if st.button(f"Add {kind}", key=f"add_{kind}:{view.candidate_id}"):
        _snapshot_undo(buffer)
        buffer[value_key].append("")
        buffer[ids_key].append(str(uuid.uuid4()))
        _update_raw_section(buffer, field)
        st.rerun()
    bulk_open_key = f"_bulk_open_{field}"
    if not buffer.get(bulk_open_key, False):
        if st.button(f"Bulk edit {field}", key=f"open_bulk_{field}:{view.candidate_id}"):
            buffer[bulk_open_key] = True
    if buffer.get(bulk_open_key, False):
        bulk_editor = st.empty()
        with bulk_editor.container(border=True):
            _render_bulk_editor(view, buffer, field)
        if not buffer.get(bulk_open_key, False):
            bulk_editor.empty()
            st.button(f"Bulk edit {field}", key=f"open_bulk_{field}:{view.candidate_id}")


def _snapshot_undo(buffer) -> None:
    buffer["_undo"] = {
        "structured_ingredients": list(buffer.get("structured_ingredients", [])),
        "structured_instructions": list(buffer.get("structured_instructions", [])),
        "ingredient_ids": list(buffer.get("ingredient_ids", [])),
        "instruction_ids": list(buffer.get("instruction_ids", [])),
    }


def _mutate_row(view, buffer, values_key: str, ids_key: str, index: int, action: str) -> None:
    _snapshot_undo(buffer)
    values, ids = buffer[values_key], buffer[ids_key]
    if action == "Insert below":
        values.insert(index + 1, "")
        ids.insert(index + 1, str(uuid.uuid4()))
    elif action == "Move up" and index > 0:
        values[index - 1:index + 1] = reversed(values[index - 1:index + 1])
        ids[index - 1:index + 1] = reversed(ids[index - 1:index + 1])
    elif action == "Move down" and index + 1 < len(values):
        values[index:index + 2] = reversed(values[index:index + 2])
        ids[index:index + 2] = reversed(ids[index:index + 2])
    elif action == "Merge with next" and values_key == "structured_instructions" and index + 1 < len(values):
        values[index:index + 2] = [values[index] + "\n\n" + values[index + 1]]
        ids[index:index + 2] = [ids[index]]
    elif action == "Remove":
        values.pop(index)
        ids.pop(index)
    st.rerun()


def _render_suggestions(view, buffer, field: str, row_id: str | None = None) -> None:
    for suggestion in view.pending_suggestions:
        path = suggestion.field_path
        if path != field and not path.startswith(field + "["):
            continue
        binding = next((item for item in view.target_bindings
                        if item.get("issue_id") == suggestion.suggestion_id), None)
        target_id = binding.get("source_target_id") if binding else None
        target_kind = binding.get("source_target_kind") if binding else None
        row_ids = buffer.get("ingredient_ids" if field == "ingredients" else "instruction_ids", [])
        exact_row = (target_kind == "row" and target_id in row_ids)
        whole_field = path == field and target_kind == "field" and bool(target_id)
        if row_id is not None:
            if not exact_row or target_id != row_id:
                continue
        elif exact_row:
            continue
        can_apply = exact_row or whole_field
        with st.container(border=True):
            st.markdown(f"**Unverified proposal · {path}**")
            if not can_apply:
                st.caption("No exact original row is available. Review the scan; this proposal cannot be applied by position.")
            st.write(suggestion.proposed_value)
            st.caption(f"Why: {suggestion.rationale} · Uncertainty: {suggestion.uncertainty or 'Not stated'} · Confidence: {suggestion.confidence}")
            editing_key = f"suggestion_editing:{view.candidate_id}:{suggestion.suggestion_id}"
            if st.session_state.get(editing_key):
                value_key = f"suggestion_edit:{view.candidate_id}:{suggestion.suggestion_id}"
                st.session_state.setdefault(
                    value_key,
                    buffer.get("_suggestion_edits", {}).get(suggestion.suggestion_id, suggestion.proposed_value),
                )
                edited = st.text_area("Edit suggestion", key=value_key, height=88)
                actions = st.columns(2)
                if actions[0].button("Use edited version", key=f"edit_s:{view.candidate_id}:{suggestion.suggestion_id}",
                                     disabled=not can_apply or not edited.strip()):
                    _run_issue_action(lambda: _apply_proposal(view, suggestion, "edited", edited, buffer))
                if actions[1].button("Cancel edit", key=f"cancel_edit_s:{view.candidate_id}:{suggestion.suggestion_id}"):
                    st.session_state.pop(editing_key, None)
                    st.session_state.pop(value_key, None)
                    st.rerun()
            else:
                actions = st.columns(3)
                if actions[0].button("Use suggestion", key=f"approve_s:{view.candidate_id}:{suggestion.suggestion_id}",
                                     disabled=not can_apply):
                    _run_issue_action(lambda: _apply_proposal(view, suggestion, "accepted", suggestion.proposed_value, buffer))
                if actions[1].button("Edit suggestion", key=f"start_edit_s:{view.candidate_id}:{suggestion.suggestion_id}"):
                    st.session_state[editing_key] = True
                    st.session_state[f"suggestion_edit:{view.candidate_id}:{suggestion.suggestion_id}"] = suggestion.proposed_value
                    buffer.setdefault("_suggestion_edits", {})[suggestion.suggestion_id] = suggestion.proposed_value
                    st.rerun()
                if actions[2].button("Reject", key=f"reject_s:{view.candidate_id}:{suggestion.suggestion_id}"):
                    _run_issue_action(lambda: _apply_proposal(view, suggestion, "rejected", None, buffer))


def _render_findings(view, buffer) -> None:
    issue_count = len(view.critical_findings) + len(view.pending_suggestions)
    with st.expander(f"Issues and source notes · {issue_count} unresolved", expanded=issue_count > 0):
        if view.warnings:
            st.markdown("**Source and OCR notes**")
            for warning in view.warnings:
                st.warning(warning)
        for index, finding in enumerate(view.critical_findings):
            with st.container(border=True):
                st.warning(f"{finding.field_path}: {finding.detail}")
                if finding.source_evidence:
                    st.caption(f"Source evidence: {finding.source_evidence}")
                st.caption(f"Next: {finding.suggested_action}")
                if st.button("I corrected this from the source", key=f"resolve_finding:{view.candidate_id}:{index}"):
                    _run_issue_action(lambda: _resolve_finding(view, buffer, finding, index))
        for suggestion in view.pending_suggestions:
            if suggestion.field_path in {"ingredients", "instructions"} or suggestion.field_path.startswith(("ingredients[", "instructions[")):
                continue
            with st.container(border=True):
                st.markdown(f"**Unverified proposal · {suggestion.field_path}**")
                st.write(suggestion.proposed_value)
                st.caption(f"Why: {suggestion.rationale} · Uncertainty: {suggestion.uncertainty or 'Not stated'} · Confidence: {suggestion.confidence}")
                editing_key = f"editing_scalar_suggestion:{view.candidate_id}:{suggestion.suggestion_id}"
                value_key = f"suggestion:{view.candidate_id}:{suggestion.suggestion_id}"
                if st.session_state.get(editing_key):
                    st.session_state.setdefault(value_key, suggestion.proposed_value)
                    edited = st.text_area("Edit suggestion", key=value_key, height=88)
                    actions = st.columns(3)
                    if actions[0].button("Use suggestion", key=f"approve_s:{view.candidate_id}:{suggestion.suggestion_id}"):
                        _run_issue_action(lambda: _resolve_suggestion(view, suggestion, "accepted", suggestion.proposed_value, buffer))
                    if actions[1].button("Use edited version", key=f"edit_s:{view.candidate_id}:{suggestion.suggestion_id}",
                                         disabled=not edited.strip()):
                        _run_issue_action(lambda: _resolve_suggestion(view, suggestion, "edited", edited, buffer))
                    if actions[2].button("Reject", key=f"reject_s:{view.candidate_id}:{suggestion.suggestion_id}"):
                        _run_issue_action(lambda: _resolve_suggestion(view, suggestion, "rejected", None, buffer))
                else:
                    actions = st.columns(3)
                    if actions[0].button("Use suggestion", key=f"approve_s:{view.candidate_id}:{suggestion.suggestion_id}"):
                        _run_issue_action(lambda: _resolve_suggestion(view, suggestion, "accepted", suggestion.proposed_value, buffer))
                    if actions[1].button("Edit suggestion", key=f"start_edit_s:{view.candidate_id}:{suggestion.suggestion_id}"):
                        st.session_state[editing_key] = True
                        st.session_state[value_key] = suggestion.proposed_value
                        st.rerun()
                    if actions[2].button("Reject", key=f"reject_s:{view.candidate_id}:{suggestion.suggestion_id}"):
                        _run_issue_action(lambda: _resolve_suggestion(view, suggestion, "rejected", None, buffer))
                current_value = _current_value_for_path(view.candidate, suggestion.field_path)
                if current_value is not None:
                    if st.button("Keep verified current value", key=f"keep_current:{view.candidate_id}:{suggestion.suggestion_id}"):
                        _run_issue_action(lambda: _resolve_suggestion(
                            view, suggestion, "source_corrected", current_value, buffer))
                evidence = st.text_input("Source note for optional omission",
                                         key=f"omission_evidence:{view.candidate_id}:{suggestion.suggestion_id}")
                if st.button("Record optional omission", key=f"omit_s:{view.candidate_id}:{suggestion.suggestion_id}",
                             disabled=not evidence.strip()):
                    _run_issue_action(lambda: _resolve_suggestion(
                        view, suggestion, "not_recipe_content", None, buffer, evidence.strip()))
                st.caption("Rejecting a proposal alone never clears a separate critical source finding.")
    if buffer.get("_undo"):
        if st.button("Undo last edit", key=f"undo:{view.candidate_id}"):
            undo = buffer.pop("_undo")
            buffer.update(undo)
            try:
                record, _ = save_draft(view, buffer)
                st.session_state.review_save_state = f"Undo saved locally · revision {record.revision}"
            except Exception as exc:
                st.error(f"Undo could not be saved; your current buffer is retained. {exc}")
                buffer["_undo"] = undo
                return
            st.rerun()


def _current_value_for_path(candidate, field_path: str) -> str | None:
    import re
    match = re.fullmatch(r"ingredients\[(\d+)\]", field_path)
    if match:
        index = int(match.group(1))
        return candidate.ingredients[index] if index < len(candidate.ingredients) else None
    if field_path == "yield_text":
        return candidate.yield_text or ""
    if field_path == "description":
        return candidate.description or ""
    return None



main()
