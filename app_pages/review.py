"""M1 Review workspace: compare source, correct safely, and record decisions."""
from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from pathlib import Path
import streamlit as st

from app_pages.common import (
    CatalogReadError,
    buffer_for,
    buffer_is_dirty,
    enqueue_suggestion_request,
    suggestion_service,
    candidate_from_buffer,
    catalog,
    load_resume_pointer,
    refresh_buffer_after_decision,
    retry_service,
    review_store,
    save_repair_proposal,
    save_resume_pointer,
    save_draft,
    structured_widget_key,
    sync_buffer,
    paths,
    _buffer_signature,
    _revision_bindings,
)
from ui_data import (extraction_quality_audit, render_source_page,
                     source_text_for_page, verify_selected_source)
from review_queue_state import QueueState
from recipe_quality import is_complete
from recipe_validation import propose_repair, validate_recipe
from suggestion_ui import (
    approval_blockers, render_approval_blockers, render_critical_finding_resolution,
    render_suggestion_card, render_suggestion_summary, suggestion_current_value,
    suggestion_status_label,
)
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
    generated = bool(getattr(suggestion, "generated", False))
    if generated:
        store = review_store()
        proposal_id = str(getattr(suggestion, "suggestion_id", ""))
        operation_id = f"ui-dynamic:{view.candidate_id}:{proposal_id}:{disposition}:{uuid.uuid4().hex}"
        if disposition == "discard_stale":
            store.discard_stale_dynamic_suggestion(
                candidate_id=view.candidate_id, proposal_id=proposal_id,
                operation_id=operation_id,
            )
        elif disposition in {"accepted", "edited"}:
            candidate, _warnings = candidate_from_buffer(view, buffer)
            store.apply_dynamic_suggestion(
                candidate_id=view.candidate_id, proposal_id=proposal_id,
                expected_parent=view.revision, recipe=candidate.model_dump(mode="json"),
                target_bindings=_revision_bindings(view, buffer, candidate),
                disposition=disposition, final_value=final_value,
                operation_id=operation_id,
            )
        else:
            store.resolve_dynamic_suggestion(
                candidate_id=view.candidate_id, proposal_id=proposal_id,
                disposition=disposition, final_value=final_value,
                operation_id=operation_id,
            )
        st.session_state.review_refresh_candidate = view.candidate_id
        return

    store = review_store()
    binding = next((item for item in view.target_bindings
                    if item.get("issue_id") == suggestion.suggestion_id), None)
    if binding is None:
        raise ValueError("The proposal has no unambiguous registered target.")
    saved, _ = save_draft(view, buffer)
    digest = hashlib.sha256((disposition + str(final_value or "") + evidence_reference).encode("utf-8")).hexdigest()[:20]
    store.resolve_issue(
        candidate_id=view.candidate_id, source_hash=view.source_hash,
        extraction_id=view.extraction_id, revision=saved.revision,
        issue_id=suggestion.suggestion_id, target_id=binding["target_id"],
        disposition=disposition, final_value=final_value,
        operation_id=f"ui-issue:{view.candidate_id}:{saved.revision}:{suggestion.suggestion_id}:{digest}",
        evidence_reference=evidence_reference,
    )
    st.session_state.review_refresh_candidate = view.candidate_id


def _apply_proposal(view, suggestion, disposition: str, final_value: str | None, buffer) -> None:
    generated = bool(getattr(suggestion, "generated", False))
    evidence_reference = ""
    if disposition == "declined" and not generated:
        field_path = str(getattr(suggestion, "field_path", ""))
        has_critical_finding = any(
            finding.severity == "critical" and finding.field_path == field_path
            for finding in view.critical_findings
        )
        if has_critical_finding:
            # Keep a source-backed critical issue open until its own explicit
            # correction or completion action is recorded.
            disposition = "rejected"
        else:
            # Optional metadata can be declined with a clear, durable reason.
            disposition = "not_recipe_content"
            evidence_reference = "Reviewer declined this optional completion; no source content was added."
    if disposition in {"accepted", "edited"}:
        binding = next((item for item in view.target_bindings
                        if item.get("issue_id") == suggestion.suggestion_id), None)
        target_id = (getattr(suggestion, "target_id", None) if generated else
                     binding.get("source_target_id") if binding else None)
        target_kind = binding.get("source_target_kind") if binding else ("row" if target_id else "field")
        field = binding.get("field") if binding else "instructions"
        if target_kind == "row" and field in {"ingredients", "instructions"}:
            ids_key = "ingredient_ids" if field == "ingredients" else "instruction_ids"
            values_key = "structured_ingredients" if field == "ingredients" else "structured_instructions"
            row_ids = buffer.get(ids_key, [])
            if not target_id or target_id not in row_ids:
                raise ValueError("The original recipe row is no longer present; this proposal cannot be applied by position.")
            buffer[values_key][row_ids.index(target_id)] = str(final_value or "")
            _update_raw_section(buffer, field)
        elif generated and field == "instructions":
            values = buffer.setdefault("structured_instructions", [])
            ids = buffer.setdefault("instruction_ids", [])
            location = getattr(suggestion, "insertion_location", {}) or {}
            index = location.get("index")
            if index is None:
                index = len(values)
            if not isinstance(index, int) or index < 0 or index > len(values):
                raise ValueError("The requested instruction location is no longer available.")
            values.insert(index, str(final_value or ""))
            ids.insert(index, str(uuid.uuid4()))
            _update_raw_section(buffer, field)
        elif target_kind == "field" and field in {"ingredients", "instructions"}:
            from quick_edit import parse_ingredients, parse_instructions
            parsed = parse_ingredients(str(final_value or "")) if field == "ingredients" else parse_instructions(str(final_value or ""))
            buffer[f"structured_{field}"] = list(parsed.items)
            buffer["ingredient_ids" if field == "ingredients" else "instruction_ids"] = [
                str(uuid.uuid4()) for _ in parsed.items
            ]
            _update_raw_section(buffer, field)
        elif target_kind == "field" and field in {"name", "yield_text", "description"}:
            buffer[field] = str(final_value or "")
        else:
            raise ValueError("The proposal has no exact ingredient or instruction target.")
    _resolve_suggestion(view, suggestion, disposition, final_value, buffer,
                        evidence_reference=evidence_reference)


def _resolve_finding(view, buffer, finding, index: int, disposition: str = "source_corrected",
                     evidence_reference: str = "") -> None:
    binding = next((item for item in view.target_bindings
                    if item.get("target_kind") == "finding"
                    and item.get("field_path_at_extraction") == finding.field_path), None)
    if binding is None:
        raise ValueError("The source issue has no unambiguous registered target.")
    if disposition == "deferred":
        st.session_state.review_save_state = "Review remains open. The source issue has not been cleared."
        return
    saved, _ = save_draft(view, buffer)
    current_candidate, _warnings = candidate_from_buffer(view, buffer)
    review_store().resolve_issue(
        candidate_id=view.candidate_id, source_hash=view.source_hash,
        extraction_id=view.extraction_id, revision=saved.revision,
        issue_id=binding["issue_id"], target_id=binding["target_id"],
        disposition=disposition,
        final_value=(_current_value_for_path(current_candidate, finding.field_path)
                     if disposition == "human_completion" else None),
        operation_id=f"ui-finding:{view.candidate_id}:{saved.revision}:{index}:{disposition}",
        evidence_reference=evidence_reference,
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
    candidate, _ = candidate_from_buffer(view, buffer)
    blockers = _approval_blockers(view, candidate)
    return blockers[0].label if blockers else None


def _approval_blockers(view, candidate=None):
    request = _suggestion_request_snapshot(view)
    return approval_blockers(
        view, candidate, suggestion_request_status=request.get("status") if request else None,
        no_instructions_confirmed=bool(view.no_instructions_reason and candidate is not None
                                       and candidate.model_dump(mode="json") == view.candidate.model_dump(mode="json")))


def _suggestion_request_snapshot(view):
    """Seed from catalog state and poll durable storage only for active work."""
    candidate_id = view.candidate_id
    key = f"suggestion_request_snapshot:{candidate_id}"
    cached = st.session_state.get(key)
    identity = (view.source_hash, view.extraction_id)
    now = time.monotonic()
    catalog_request = view.latest_suggestion_request
    if cached and cached.get("identity") == identity:
        request = cached.get("request")
        if catalog_request and (not request or
                                catalog_request.get("request_id") != request.get("request_id")):
            request = catalog_request
            cached = {"identity": identity, "checked_at": now, "request": request}
            st.session_state[key] = cached
        status = request.get("status") if request else None
        if status not in {"queued", "running"}:
            return request
        if now - cached.get("checked_at", 0.0) < 2.0:
            return request
        request = suggestion_service().status(request["request_id"])
    else:
        request = catalog_request
    st.session_state[key] = {"identity": identity, "checked_at": now, "request": request}
    return request


def _invalidate_suggestion_request_snapshot(candidate_id: str) -> None:
    st.session_state.pop(f"suggestion_request_snapshot:{candidate_id}", None)


def _jump_to_blocker(anchor: str, candidate_id: str, blocker_code: str = "") -> None:
    st.session_state.review_pending_blocker_jump = {
        "candidate_id": candidate_id, "anchor": anchor, "blocker_code": blocker_code,
    }
    st.rerun(scope="app")


def _apply_pending_blocker_jump(views) -> None:
    """Route before widgets render, retaining the selected candidate and its draft."""
    jump = st.session_state.pop("review_pending_blocker_jump", None)
    if not isinstance(jump, dict):
        return
    candidate_id = str(jump.get("candidate_id", ""))
    anchor = str(jump.get("anchor", ""))
    blocker_code = str(jump.get("blocker_code", ""))
    target = next((view for view in views if view.candidate_id == candidate_id), None)
    if target is None:
        st.session_state.review_transition_error = "That review item is no longer available in the current library."
        return
    current_id = st.session_state.get("review_active_selection", st.session_state.get("review_selection"))
    if current_id and current_id != candidate_id and not _flush_candidate(current_id, views):
        st.session_state.review_selection = current_id
        return

    queue_page = _queue_page(views)
    if candidate_id not in queue_page["all_ids"]:
        st.session_state.review_blocker_return_context = _navigation_context()
        for key, value in (("review_queue", "All recipes"), ("review_search", ""),
                           ("review_source", "All source files"), ("review_status", "Any status")):
            st.session_state[key] = value
            st.session_state.setdefault("review_filter_snapshot", {})[key] = value
        queue_page = _queue_page(views)
    target_index = queue_page["all_ids"].index(candidate_id)
    st.session_state.review_page = target_index // 25 + 1
    st.session_state.review_selection = candidate_id

    if anchor == "recipe-identity":
        st.session_state.review_identity_target = candidate_id
    if anchor == "protected-outcome":
        st.session_state.review_protected_target = candidate_id
        st.session_state.review_workspace_view = "Recipe"
    if anchor in {"critical-findings", "suggestions", "suggestion-request"}:
        st.session_state.review_show_issues = True
        if anchor == "suggestions":
            st.session_state.review_workspace_view = "Recipe"
        if anchor == "critical-findings" and target.critical_findings:
            finding = target.critical_findings[0]
            st.session_state.review_finding_target = (candidate_id, finding.field_path)
        if anchor == "suggestions":
            expected_status = {"stale": "stale", "target_missing": "target_missing",
                               "suggestions": "pending"}.get(blocker_code)
            unresolved = next((item for item in target.candidate.suggestions
                               if (getattr(item, "dynamic_status", None)
                                   or getattr(item, "review_status", "pending")) == expected_status), None)
            if unresolved:
                st.session_state.review_suggestion_target = (candidate_id, unresolved.suggestion_id)
                field_path = str(unresolved.field_path).casefold()
                section = ("Ingredients" if field_path.startswith("ingredients") else
                           "Instructions" if field_path.startswith("instructions") else None)
                if section:
                    st.session_state[f"review_pending_section:{candidate_id}"] = section
    if anchor == "source-recovery":
        st.session_state.review_workspace_view = "Source"
    elif anchor in {"recipe-title", "recipe-ingredients", "recipe-instructions", "recipe-editor"}:
        st.session_state.review_workspace_view = "Recipe"
        if anchor == "recipe-title":
            st.session_state.review_focus_field = "title"
        else:
            st.session_state[f"review_pending_section:{candidate_id}"] = (
                "Ingredients" if anchor == "recipe-ingredients" else
                "Instructions" if anchor == "recipe-instructions" else "Ingredients")


def _restore_blocker_queue() -> None:
    context = st.session_state.pop("review_blocker_return_context", None)
    if not isinstance(context, dict):
        return
    for key, value in context.items():
        if value is not None:
            st.session_state[key] = value
    st.session_state.review_page = context.get("review_page", 1) or 1
    st.rerun(scope="app")


def _queue_state() -> QueueState:
    from app_pages.common import paths
    output, _ = paths()
    return QueueState(output)


def _navigation_context() -> dict:
    return {key: st.session_state.get(key) for key in (
        "review_queue", "review_search", "review_source", "review_status", "review_page")}


def _request_recipe_title_focus() -> None:
    st.session_state.review_focus_request = int(
        st.session_state.get("review_focus_request", 0)
    ) + 1


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
    st.session_state.review_pending_advance = {"direction": direction, "skip": direction > 0}


def _back_callback() -> None:
    current = st.session_state.get("review_selection", st.session_state.get("review_active_selection"))
    st.session_state.review_pending_return = _queue_state().back(current, record=True)


def _open_skipped_callback() -> None:
    st.session_state.review_show_skipped = True


def _return_from_skipped(candidate_id: str, context: dict) -> None:
    st.session_state.review_show_skipped = False
    st.session_state.review_pending_return = {"candidate_id": candidate_id, "context": context}


def _render_skipped_list() -> None:
    st.title("Skipped recipes")
    st.caption("Skipped recipes remain here until you decide what to do with them.")
    st.button("Back to review", key="close_skipped", on_click=lambda: st.session_state.update(review_show_skipped=False))
    for index, item in enumerate(reversed(_queue_state().skipped())):
        st.button(f"Return to {item['title']}", key=f"return_skipped:{index}:{item['candidate_id']}",
                  on_click=_return_from_skipped, args=(item["candidate_id"], item.get("context", {})))


def _apply_pending_advance(views) -> None:
    return_event = st.session_state.pop("review_pending_return", None)
    pending = st.session_state.pop("review_pending_advance", None)
    if pending is None and return_event is None:
        return
    if pending is None:
        pending = {"direction": 0, "return": return_event}
    direction = pending["direction"] if isinstance(pending, dict) else pending
    current_id = st.session_state.get("review_active_selection", st.session_state.get("review_selection"))
    if current_id and not _flush_candidate(current_id, views):
        st.session_state.review_selection = current_id
        if isinstance(pending, dict) and pending.get("return"):
            st.session_state.review_pending_return = pending["return"]
        return
    if isinstance(pending, dict) and pending.get("return"):
        event = pending["return"]
        for key, value in event.get("context", {}).items():
            if value is not None:
                st.session_state[key] = value
        _route_return_target(views, event["candidate_id"])
        st.session_state.review_selection = event["candidate_id"]
        return
    if pending.get("skip") if isinstance(pending, dict) else False:
        current_view = next((item for item in views if item.candidate_id == current_id), None)
        if current_view:
            _queue_state().record_skip(current_id, current_view.candidate.name, _navigation_context())
    ids = st.session_state.get("review_all_queue_ids", st.session_state.get("review_queue_ids", []))
    current = st.session_state.get("review_selection")
    if not ids:
        st.session_state.review_selection = None
    elif current is None:
        if direction < 0:
            st.session_state.review_selection = ids[-1]
    else:
        index = ids.index(current) if current in ids else 0
        target = index + direction
        st.session_state.review_selection = None if target < 0 or target >= len(ids) else ids[target]
        if st.session_state.review_selection:
            target_index = ids.index(st.session_state.review_selection)
            st.session_state.review_page = target_index // 25 + 1


def _route_return_target(views, candidate_id: str) -> None:
    """Keep explicit Back/skipped targets visible even if their old filters changed."""
    queue_page = _queue_page(views)
    all_ids = queue_page["all_ids"]
    if candidate_id not in all_ids:
        st.session_state.review_queue = "All recipes"
        st.session_state.review_search = ""
        st.session_state.review_source = "All source files"
        st.session_state.review_status = "Any status"
        st.session_state.review_filter_restore_notice = (
            "Showing the requested recipe outside the saved queue filters."
        )
        queue_page = _queue_page(views)
        all_ids = queue_page["all_ids"]
    st.session_state.review_filter_snapshot = {
        key: st.session_state.get(key) for key in (
            "review_queue", "review_search", "review_source", "review_status")
    }
    if candidate_id in all_ids:
        st.session_state.review_page = all_ids.index(candidate_id) // 25 + 1


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
    previous_revision = int(buffer.get("_revision", view.revision))
    feedback = st.session_state.get("review_save_state")
    if isinstance(feedback, dict) and feedback.get("kind") == "draft_saved":
        st.session_state.pop("review_save_state", None)
    try:
        record, _ = save_draft(view, buffer)
    except Exception as exc:
        st.error(f"Draft was not saved. Your current text remains on screen: {exc}")
        return
    message = (f"Draft saved (revision {record.revision})."
               if record.revision != previous_revision
               else f"Draft already saved (revision {record.revision}).")
    st.session_state.review_save_state = {
        "kind": "draft_saved",
        "candidate_id": view.candidate_id,
        "revision": record.revision,
        "signature": buffer.get("_saved_signature"),
        "message": message,
    }
    st.session_state.review_active_selection = view.candidate_id
    _request_recipe_title_focus()
    if record.revision == view.revision and advance:
        _advance_queue(1)
    st.rerun()


def _approve(view, buffer, *, approval_method: str = "source_verified",
             reviewer_acknowledged: bool = False, review_rationale: str = "") -> None:
    candidate, _ = candidate_from_buffer(view, buffer)
    no_instructions_confirmed = bool(
        view.no_instructions_reason
        and candidate.model_dump(mode="json") == view.candidate.model_dump(mode="json"))
    if not is_complete(candidate, no_instructions_confirmed=no_instructions_confirmed):
        st.error("Add instructions or record why no preparation instructions are needed before approval.")
        return
    if any(item.severity == "critical" for item in candidate.missing_information):
        st.error("Resolve the critical source warning before approving this recipe.")
        return
    if any((getattr(item, "dynamic_status", None) or item.review_status)
           in {"pending", "stale", "target_missing", "rejected"} for item in candidate.suggestions):
        st.error("Resolve each unverified proposal or source issue before approving this recipe.")
        return
    saved = None
    try:
        saved, _ = save_draft(view, buffer)
        store = review_store()
        store.approve_revision(
            candidate_id=view.candidate_id, source_hash=view.source_hash,
            extraction_id=view.extraction_id, revision=saved.revision,
            operation_id=(f"ui-approve:{view.candidate_id}:{view.extraction_id}:"
                          f"{saved.revision}:{approval_method}"),
            source_path=view.source_path,
            approval_method=approval_method,
            reviewer_acknowledged=reviewer_acknowledged,
            review_rationale=review_rationale,
        )
    except Exception as exc:
        if saved is not None:
            st.error(f"Draft saved at revision {saved.revision}; approval was not recorded. This recipe remains selected. Retry approval after checking the source. {exc}")
        else:
            st.error(f"Approval was not saved. Your current text remains on screen: {exc}")
        return
    _request_recipe_title_focus()
    _advance_queue(1)


def _render_manual_approval(view, buffer) -> None:
    if view.protected:
        return
    with st.container(border=True):
        st.markdown("**Manually verify the current recipe**")
        st.caption("This approval records that you reviewed the recipe as written. It does not verify the transcription against the original scan.")
        st.markdown(f"**Saved revision {view.revision}: {view.candidate.name or 'Untitled recipe'}**")
        st.write("**Description**", view.candidate.description or "Not recorded")
        st.write("**Yield**", view.candidate.yield_text or "Not recorded")
        for label, values, empty in (
            ("Ingredients", view.candidate.ingredients, "No ingredients recorded"),
            ("Instructions", view.candidate.instructions, "No instructions recorded"),
            ("Notes", view.candidate.notes, "No notes recorded"),
        ):
            st.markdown(f"**{label}**")
            st.text("\n".join(values) if values else empty)
        st.write("**Warnings**", view.candidate.warnings or ["No warnings recorded"])
        if view.candidate.uncertainties:
            st.warning("Uncertainties: " + " · ".join(view.candidate.uncertainties))
        if view.critical_findings:
            st.warning("Outstanding critical findings must be explicitly resolved before approval.")
            for finding in view.critical_findings:
                st.caption(f"{finding.field_path}: {finding.detail}")
        candidate, _ = candidate_from_buffer(view, buffer)
        saved_candidate = view.candidate.model_dump(mode="json")
        buffer_matches = candidate.model_dump(mode="json") == saved_candidate
        if view.revision == 0:
            if buffer_matches:
                st.warning("Save a review revision before manual approval. Reopen this preview after the saved revision is loaded, then acknowledge that exact recipe.")
            else:
                st.warning("The editor has unsaved changes. Save them as the first review revision before opening manual approval.")
            if st.button("Save revision for manual review",
                         key=f"save_manual_revision:{view.candidate_id}"):
                _save(view, buffer, advance=False)
                st.rerun(scope="app")
            st.button("Approve manually & next", key=f"approve_manual:{view.candidate_id}",
                      disabled=True, type="primary", width="stretch")
            return
        if view.protected:
            st.warning("This recipe has a protected Mealie outcome and cannot be approved again.")
            return
        blockers = [item for item in _approval_blockers(view, candidate) if item.code != "source"]
        if blockers:
            st.caption("Resolve the other approval blockers first: " + "; ".join(item.label for item in blockers))
        if not buffer_matches:
            st.warning("The editor has unsaved changes. This preview shows the saved revision only; save the draft to review and approve its new revision.")
            if st.button("Save draft and reopen approval preview",
                         key=f"save_manual_preview:{view.candidate_id}"):
                _save(view, buffer, advance=False)
                st.rerun(scope="app")
        context = st.session_state.get("manual_approval_context")
        if context != (view.candidate_id, view.revision, view.content_digest):
            st.session_state.manual_approval_context = (
                view.candidate_id, view.revision, view.content_digest)
            st.session_state.manual_approval_nonce = uuid.uuid4().hex
        nonce = st.session_state.manual_approval_nonce
        ack_key = f"manual_approval_ack:{view.candidate_id}:{view.revision}:{view.content_digest}:{nonce}"
        rationale_key = f"manual_approval_rationale:{view.candidate_id}:{view.revision}:{view.content_digest}:{nonce}"
        acknowledged = st.checkbox(
            "I have reviewed the recipe as currently written. I understand that the original source is unavailable and that this approval does not verify the transcription against the original document.",
            key=ack_key,
            disabled=not buffer_matches,
        )
        rationale = st.text_area("Why are you approving this recipe without its original source?",
                                 key=rationale_key, height=90, disabled=not buffer_matches)
        if st.button("Approve manually & next", key=f"approve_manual:{view.candidate_id}",
                     disabled=bool(blockers) or not buffer_matches or not acknowledged or not rationale.strip(),
                     type="primary", width="stretch"):
            try:
                store = review_store()
                store._reload()
                latest = max((item for item in store.revisions.get(view.candidate_id, [])
                              if item.get("source_hash") == view.source_hash
                              and item.get("extraction_id") == view.extraction_id),
                             key=lambda item: int(item["revision"]), default=None)
                if (latest is None or int(latest["revision"]) != view.revision
                        or latest.get("content_sha256") != view.content_digest
                        or latest.get("recipe") != saved_candidate
                        or candidate.model_dump(mode="json") != saved_candidate):
                    raise ValueError("The saved revision changed or the editor no longer matches this preview. Refresh and review the latest saved revision.")
                store.approve_revision(
                    candidate_id=view.candidate_id, source_hash=view.source_hash,
                    extraction_id=view.extraction_id, revision=view.revision,
                    operation_id=(f"ui-manual-approve:{view.candidate_id}:{view.extraction_id}:"
                                  f"{view.revision}:{view.content_digest}"),
                    source_path=view.source_path,
                    approval_method="manual_source_unavailable",
                    reviewer_acknowledged=acknowledged, review_rationale=rationale,
                )
                _advance_queue(1)
            except Exception as exc:
                st.error(f"Manual approval was not recorded. The saved revision remains selected: {exc}")


def _navigate_to_manual_instructions(candidate_id: str) -> None:
    if st.session_state.get(f"instruction_handling:{candidate_id}") != "Add instructions manually":
        return
    st.session_state[f"recipe_sections:{candidate_id}"] = "Instructions"
    st.session_state[f"recipe_section_saved:{candidate_id}"] = "Instructions"


def _render_no_instructions_control(view, buffer) -> None:
    candidate, _ = candidate_from_buffer(view, buffer)
    if any(str(item).strip() for item in candidate.instructions) or view.protected:
        return
    st.markdown("**How should this recipe be handled?**")
    choice = st.radio(
        "Choose what to do with missing instructions",
        ["Add instructions manually", "Request AI-suggested instructions",
         "Confirm that no instructions are needed", "Keep as incomplete draft"],
        index=3,
        key=f"instruction_handling:{view.candidate_id}",
        label_visibility="collapsed",
        on_change=_navigate_to_manual_instructions,
        args=(view.candidate_id,),
    )
    if choice == "Add instructions manually":
        st.info("The Instructions section is open. Add the preparation steps in the editor above.")
    elif choice == "Request AI-suggested instructions":
        st.info("The Instructions section is open. Use its suggestion controls above.")
    elif choice == "Confirm that no instructions are needed":
        reason = st.selectbox(
            "Why are no preparation instructions required?",
            ["Dry ingredient blend", "Ready-to-use mixture", "Instructions intentionally omitted", "Other"],
            key=f"no_instructions_reason_choice:{view.candidate_id}",
        )
        explanation = ""
        if reason == "Other":
            explanation = st.text_input("Explain why no instructions are required",
                                        key=f"no_instructions_reason_other:{view.candidate_id}")
        final_reason = explanation.strip() if reason == "Other" else reason
        if st.button("Record no-instructions decision", key=f"confirm_no_instructions:{view.candidate_id}",
                     disabled=not final_reason):
            try:
                saved, _ = save_draft(view, buffer)
                reason_digest = hashlib.sha256(final_reason.encode("utf-8")).hexdigest()[:12]
                review_store().record_no_instructions_decision(
                    candidate_id=view.candidate_id, source_hash=view.source_hash,
                    extraction_id=view.extraction_id, revision=saved.revision,
                    reason=final_reason,
                    operation_id=(f"ui-no-instructions:{view.candidate_id}:{view.extraction_id}:"
                                  f"{saved.revision}:{reason_digest}"),
                )
                st.session_state.review_save_state = (
                    f"Recorded for review revision {saved.revision}: {final_reason}.")
                st.rerun(scope="app")
            except Exception as exc:
                st.error(f"The decision was not recorded. Your recipe remains selected. {exc}")


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
        quality_audit_mode = st.session_state.get("review_queue") == "Extraction quality"
        views = catalog(read_only=quality_audit_mode)
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
    if st.session_state.get("review_show_skipped"):
        _render_skipped_list()
        return
    try:
        quality_audit_mode = st.session_state.get("review_queue") == "Extraction quality"
        views = catalog(read_only=quality_audit_mode)
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
        "{font-size: 1.25rem !important; line-height: 1.3 !important; margin: 0 !important;} "
        ".st-key-review-pane-switcher {display:none;} "
        "@media (max-width: 900px) {"
        ".st-key-review-pane-switcher {display:block;} "
        ".st-key-review_workspace [data-testid='stHorizontalBlock']:has(.st-key-review_source_pane) "
        "{display:flex; flex-direction:column; gap:.5rem;} "
        "[data-testid='stAppViewContainer']:has(.st-key-review-pane-switcher [role='radio'][aria-checked='true']:first-of-type) "
        ".st-key-review_workspace [data-testid='stColumn']:has(.st-key-review_recipe_pane), "
        "[data-testid='stAppViewContainer']:has(.st-key-review-pane-switcher [role='radio'][aria-checked='true']:nth-of-type(2)) "
        ".st-key-review_workspace [data-testid='stColumn']:has(.st-key-review_source_pane) "
        "{display:none !important;} "
        "[data-testid='stAppViewContainer']:has(.st-key-review-pane-switcher [role='radio'][aria-checked='true']:first-of-type) "
        ".st-key-review_workspace [data-testid='stColumn']:has(.st-key-review_source_pane), "
        "[data-testid='stAppViewContainer']:has(.st-key-review-pane-switcher [role='radio'][aria-checked='true']:nth-of-type(2)) "
        ".st-key-review_workspace [data-testid='stColumn']:has(.st-key-review_recipe_pane) "
        "{flex:1 1 100% !important; width:100% !important; min-width:0 !important;} "
        "}</style>",
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
    _apply_pending_blocker_jump(views)
    st.session_state.setdefault("review_selection", views[0].candidate_id)
    st.session_state.setdefault("review_workspace_view", "Source")
    if st.session_state.review_workspace_view not in {"Source", "Recipe"}:
        st.session_state.review_workspace_view = "Source"
    if st.session_state.get("review_transition_error"):
        st.error(st.session_state.pop("review_transition_error"))
    if st.session_state.get("review_filter_restore_notice"):
        st.info(st.session_state.pop("review_filter_restore_notice"))
    if st.session_state.get("review_blocker_return_context"):
        st.info("This recipe was outside your saved queue filters. The queue is showing all recipes so you can review it.")
        st.button("Return to saved queue", key="review_restore_blocker_queue",
                  on_click=_restore_blocker_queue)
    queue_page = _queue_page(views)
    candidates = queue_page["candidates"]
    if st.session_state.get("review_queue") == "Extraction quality":
        audit = _quality_audit_snapshot(views)
        metrics = st.columns(4)
        metrics[0].metric("Recipes inspected", audit["inspected"])
        metrics[1].metric("Potential issues", audit["with_issues"])
        metrics[2].metric("High priority", audit["high_priority"])
        metrics[3].metric("Needs manual review", audit["needs_manual_review"])
        st.caption(f"Saved corrections: {audit['corrected']} · Deferred: {audit['deferred']}")
    if not candidates:
        _render_queue(views, queue_page)
        st.info("No recipes match these filters. Open Review queue to adjust them.")
        controls = st.columns([1, 1.8], vertical_alignment="center")
        controls[0].button("Back", key="empty_queue_back", on_click=_back_callback,
                            disabled=_queue_state().back(st.session_state.get("review_selection")) is None,
                            width="stretch")
        controls[1].button(f"Skipped recipes ({len(_queue_state().skipped())})", key="empty_queue_skipped",
                           on_click=_open_skipped_callback, width="stretch")
        return
    if "review_selection" in st.session_state and st.session_state.review_selection is None:
        _render_queue(views, queue_page)
        st.success("You’ve reached the end of this queue.")
        cols = st.columns([1, 1.8], vertical_alignment="center")
        cols[0].button("Back", key="queue_end_back", on_click=_back_callback,
                       disabled=_queue_state().back(st.session_state.get("review_selection")) is None,
                       width="stretch")
        cols[1].button(f"Skipped recipes ({len(_queue_state().skipped())})", key="queue_end_skipped",
                       on_click=_open_skipped_callback, width="stretch")
        return
    selected_id = st.session_state.get("review_selection")
    ids = [view.candidate_id for view in candidates]
    if selected_id not in ids:
        all_ids = queue_page["all_ids"]
        if selected_id in all_ids:
            st.session_state.review_page = all_ids.index(selected_id) // 25 + 1
            queue_page = _queue_page(views)
            candidates = queue_page["candidates"]
            ids = [view.candidate_id for view in candidates]
        elif ids:
            selected_id = ids[0]
            st.session_state.review_selection = selected_id
        else:
            _render_queue(views, queue_page)
            st.info("The selected recipe is outside this queue. Adjust the filters to return to it.")
            return
    previous_active_selection = st.session_state.get("review_active_selection")
    st.session_state.review_queue_ids = ids
    st.session_state.review_all_queue_ids = queue_page["all_ids"]
    view = verify_selected_source(next(v for v in candidates if v.candidate_id == selected_id))
    pending_section_key = f"review_pending_section:{selected_id}"
    pending_section = st.session_state.pop(pending_section_key, None)
    if pending_section:
        st.session_state[f"recipe_sections:{selected_id}"] = pending_section
    buffer = buffer_for(view)
    sync_buffer(view, buffer)
    st.session_state.review_active_selection = selected_id
    if previous_active_selection and previous_active_selection != selected_id:
        _request_recipe_title_focus()
    if not quality_audit_mode:
        save_resume_pointer(view, buffer)
        _queue_state().record_visit(selected_id, view.candidate.name, _navigation_context())
    all_ids = queue_page["all_ids"]
    position = all_ids.index(selected_id) + 1
    st.markdown(f"### {view.candidate.name or 'Untitled recipe'}")
    saved_label = (f"Unsaved changes · saved revision {view.revision}"
                   if buffer_is_dirty(view, buffer) else f"Saved revision {view.revision}")
    st.caption(saved_label)
    st.caption(f"{view.status} · {position} of {len(all_ids)}")
    if view.range_provenance:
        provenance = view.range_provenance
        st.info(
            f"Cookbook: {view.source_display_name} · original pages "
            f"{provenance.get('first_page')}-{provenance.get('last_page')}. "
            "You can review and approve this recipe, but cookbook range recipes cannot be imported into Mealie in this phase."
        )
    _render_navigation(view)
    _render_suggestion_job_status(view)
    _render_queue(views, queue_page)
    if view.protected:
        st.warning("Read-only until this recipe’s Mealie outcome is reconciled.")
        if st.session_state.get("review_protected_target") == view.candidate_id:
            st.info("This prior or uncertain Mealie outcome protects the recipe from approval and another create request. Use Import history to reconcile it.")
    _render_actions(view, buffer)
    with st.container(key="review_workspace"):
        source, editor = st.columns([0.9, 1.1], gap="medium")
        with source:
            with st.container(key="review_source_pane"):
                _render_source(view, buffer)
        with editor:
            with st.container(key="review_recipe_pane"):
                _render_editor_fragment(view, buffer)
    _render_ingredient_repair(view, buffer)
    _render_findings(view, buffer)
    _render_identity_resolution(view, views)
    _render_retry_panel(view, views)


def _render_identity_resolution(view, views) -> None:
    """Show a direct, append-only identity comparison for the selected candidate."""
    if st.session_state.get("review_identity_target") != view.candidate_id:
        return
    store = review_store()
    proposals = store.identity_reconciliation_candidates()
    proposal = next((item for item in proposals if item["candidate_id"] == view.candidate_id), None)
    st.subheader("Recipe identity needs review")
    if proposal is None:
        st.info("This identity is already resolved or its saved registration is unavailable.")
        if st.button("Return to Review", key=f"identity_return:{view.candidate_id}"):
            st.session_state.pop("review_identity_target", None)
            st.rerun()
        return

    from draft_store import DraftStore
    from app_pages.common import paths
    output, _ = paths()
    drafts = DraftStore(output / "ui_drafts.jsonl").all_latest()
    previous_id = proposal.get("previous_candidate_id")
    previous_draft = drafts.get(previous_id) if previous_id else None
    held_draft = drafts.get(view.candidate_id)
    with st.container(border=True):
        st.markdown(f"**{view.candidate.name or 'Untitled recipe'}**")
        st.caption(f"Source: {view.source_display_name or 'Unknown file'} · hash `{view.source_hash[:12]}`")
        st.caption(f"Current extraction: `{view.extraction_id[:12]}` · status: needs review")
        st.caption(f"Prior import protection: {'protected' if proposal['protected'] else 'none found'}")
        st.caption(f"Saved drafts: current {'yes' if held_draft else 'no'} · previous {'yes' if previous_draft else 'no'}")
        if proposal.get("previous_candidate_id"):
            st.caption(f"Previous extraction: `{str(proposal.get('previous_extraction_id') or '')[:12]}` · status: registered")
        if not proposal["authoritative_differences"] and proposal.get("previous_candidate_id"):
            st.info("The recipe content matches an earlier extraction. The newer extraction contains different AI suggestions. These versions can be linked without automatically approving the newer suggestions.")
        elif proposal.get("possible_matches"):
            st.warning(proposal.get("reason") or "Select a possible match only after checking its source evidence.")
        else:
            st.warning("No same-source authoritative-content match is available. This identity remains held.")

        with st.expander("Inspect differences", expanded=True):
            if proposal.get("authoritative_differences"):
                st.write("Authoritative recipe fields differ:", ", ".join(proposal["authoritative_differences"]))
            elif proposal.get("previous_candidate_id"):
                st.write("Authoritative recipe fields match exactly under the documented identity rule.")
            st.write("Current registration and review activity:", proposal.get("held_history") or "No revision or decision history recorded.")
            st.write("Previous registration and review activity:", proposal.get("previous_history") or "No revision or decision history recorded.")
            for match in proposal.get("possible_matches", []):
                st.markdown(f"**Possible match:** {match['candidate'].get('name') or 'Untitled'} · `{str(match['candidate_id'])[:12]}`")
                st.caption(f"Extraction `{str(match.get('extraction_id') or '')[:12]}` · authoritative differences: {', '.join(match['differences']) or 'none'} · import protection: {'yes' if match['protected'] else 'no'}")
            current_suggestions = proposal["candidate"].get("suggestions", [])
            previous_candidate = proposal.get("previous_candidate") or {}
            previous_suggestions = previous_candidate.get("suggestions", [])
            st.write("Suggestion differences")
            if current_suggestions == previous_suggestions:
                st.caption("No suggestion differences.")
            else:
                st.json({"previous": previous_suggestions, "current": current_suggestions})
            st.caption("Diagnostic metadata differences are excluded from logical identity.")

        eligible = bool(proposal["eligible"])
        if eligible:
            st.caption("This match is uniquely eligible. Confirming it preserves both candidate histories and does not carry forward an approval.")
            if st.button("Confirm identity match", key=f"identity_confirm:{view.candidate_id}", type="primary"):
                try:
                    store.confirm_identity_match(view.candidate_id, str(previous_id))
                except Exception as exc:
                    st.error(f"This identity remains unresolved. {exc}")
                else:
                    st.session_state.pop("review_identity_target", None)
                    st.rerun()
        else:
            exact_matches = [item for item in proposal.get("possible_matches", [])
                             if not item["differences"] and item["identity_status"] == "registered"]
            if exact_matches and not proposal["protected"]:
                labels = {str(item["candidate_id"]):
                          f"{item['candidate'].get('name') or 'Untitled'} · {str(item.get('extraction_id') or '')[:12]}"
                          for item in exact_matches}
                selected = st.selectbox("Choose the exact matching extraction", list(labels),
                                        format_func=lambda key: labels[key],
                                        key=f"identity_match_choice:{view.candidate_id}")
                evidence = st.text_input("Why these source-backed records are the same recipe", key=f"identity_evidence:{view.candidate_id}")
                if st.button("Confirm selected match", key=f"identity_confirm_selected:{view.candidate_id}",
                             disabled=not evidence.strip()):
                    try:
                        store.confirm_identity_match(view.candidate_id, selected, evidence)
                    except Exception as exc:
                        st.error(f"This identity remains unresolved. {exc}")
                    else:
                        st.session_state.pop("review_identity_target", None)
                        st.rerun()
            else:
                st.caption(proposal.get("reason") or "No safe match can be confirmed from the available records.")
        batch_eligible = [item for item in proposals if item["eligible"]]
        if batch_eligible:
            st.caption(f"{len(batch_eligible)} safe identity match{'es' if len(batch_eligible) != 1 else ''} available across the review queue.")
            if st.button(f"Confirm all {len(batch_eligible)} safe matches", key="identity_confirm_batch"):
                confirmed = 0
                try:
                    for item in batch_eligible:
                        store.confirm_identity_match(item["candidate_id"], str(item["previous_candidate_id"]))
                        confirmed += 1
                except Exception as exc:
                    st.warning(f"Batch review stopped after {confirmed} confirmations. Remaining identities stay unresolved. {exc}")
                else:
                    st.session_state.pop("review_identity_target", None)
                    st.rerun()
        controls = st.columns(2)
        if controls[0].button("Defer", key=f"identity_defer:{view.candidate_id}"):
            try:
                store.defer_identity_match(view.candidate_id, view.extraction_id)
            except Exception as exc:
                st.error(f"This identity remains unresolved. {exc}")
            else:
                st.session_state.pop("review_identity_target", None)
                st.rerun()
        if controls[1].button("Return to Review", key=f"identity_return:{view.candidate_id}"):
            st.session_state.pop("review_identity_target", None)
            st.rerun()


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
                            f"ui-reconciliation:{attempt_id}:{new_id}:{target_id}:{','.join(sorted(sections))}", evidence,
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
                            f"ui-reconciliation-new:{attempt_id}:{new_id}", evidence,
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
    candidates = views if mode in {"All recipes", "Extraction quality"} else [v for v in views if not v.protected]
    if mode == "Extraction quality":
        audit = _quality_audit_snapshot(views)
        matches = audit["by_id"]
        candidates = [view for view in candidates if view.candidate_id in matches]
        category = st.session_state.get("review_quality_category", "All issue types")
        severity = st.session_state.get("review_quality_severity", "All severities")
        if category != "All issue types":
            candidates = [view for view in candidates if any(
                item.category == category for item in matches[view.candidate_id])]
        if severity != "All severities":
            desired = "critical" if severity == "High priority" else "warning"
            candidates = [view for view in candidates if any(
                item.severity == desired for item in matches[view.candidate_id])]
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
    all_candidates = candidates
    pages = max(1, (len(candidates) + 24) // 25)
    page = min(max(1, int(st.session_state.get("review_page", 1))), pages)
    st.session_state.review_page = page
    start, end = (page - 1) * 25, min(page * 25, len(candidates))
    return {"candidates": candidates[start:end], "all_ids": [v.candidate_id for v in all_candidates], "total": len(candidates),
            "pages": pages, "page": page, "start": start, "end": end}


def _render_queue(views, queue_page) -> None:
    queue_labels = ["Needs action", "All unresolved", "Extraction quality", "All recipes"]
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
        if st.session_state.review_queue == "Extraction quality":
            audit = _quality_audit_snapshot(views)
            categories = sorted({item.category for items in audit["by_id"].values() for item in items})
            if st.session_state.get("review_quality_category", "All issue types") not in ["All issue types", *categories]:
                st.session_state.review_quality_category = "All issue types"
            if st.session_state.get("review_quality_severity", "All severities") not in [
                    "All severities", "High priority", "Warnings"]:
                st.session_state.review_quality_severity = "All severities"
            filters = st.columns(2)
            filters[0].selectbox("Issue type", ["All issue types", *categories], key="review_quality_category")
            filters[1].selectbox("Severity", ["All severities", "High priority", "Warnings"], key="review_quality_severity")
            st.caption("This read-only audit checks current saved recipe revisions. Open a result to compare it with the source.")
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
            if st.session_state.review_queue == "Extraction quality":
                findings = _quality_audit_snapshot(views)["by_id"][item.candidate_id]
                st.write(findings[0].explanation)
                if item.identity_status != "registered":
                    st.caption("Editing is unavailable until this candidate has a stable review identity.")
                st.button("Review ingredient extraction", key=f"quality_open:{item.candidate_id}",
                          on_click=_select_quality_result,
                          args=(item.candidate_id, findings[0].field_path),
                          disabled=item.identity_status != "registered")


def _quality_audit_snapshot(views):
    signature = tuple((view.candidate_id, view.source_hash, view.extraction_id, view.revision,
                       view.content_digest, hashlib.sha256(json.dumps(
                           view.candidate.model_dump(mode="json"), ensure_ascii=False,
                           sort_keys=True, separators=(",", ":"),
                       ).encode("utf-8")).hexdigest(), view.status, view.protected,
                       (getattr(view.draft, "repair_proposal", {}) or {}).get("status"))
                      for view in views)
    cached = st.session_state.get("review_quality_audit")
    if cached and cached["signature"] == signature:
        return cached["audit"]
    audit = extraction_quality_audit(views)
    audit["by_id"] = {row["view"].candidate_id: row["findings"] for row in audit["rows"]}
    st.session_state.review_quality_audit = {"signature": signature, "audit": audit}
    return audit


def _select_quality_result(candidate_id: str, field_path: str) -> None:
    st.session_state.review_selection = candidate_id
    st.session_state.review_show_issues = True
    st.session_state.review_quality_target = (candidate_id, field_path)

def _render_source(view, buffer) -> None:
    output, _ = paths()
    with st.container(border=True):
        st.markdown(f"**Source scan** · {view.source_display_name}")
        if view.range_provenance:
            st.caption("Source text is read-only and separate from the editable recipe fields and any AI suggestions.")
        if not view.source_verified or not view.source_available:
            st.warning("Original source unavailable. This recipe cannot be verified against its original scanned document.")
            if view.source_path:
                st.caption(f"Last known path: {view.source_path}")
            st.write("You may restore the exact original file or manually approve the recipe after reviewing its current content.")
            uploaded = st.file_uploader(
                "Locate the original PDF or image",
                type=["pdf", "jpg", "jpeg", "png", "webp"],
                key=f"restore_source_upload:{view.candidate_id}",
            )
            if uploaded is not None:
                source_bytes = uploaded.getvalue()
                if hashlib.sha256(source_bytes).hexdigest() != view.source_hash:
                    st.error("This file does not match the original source hash. It was not saved or linked.")
                else:
                    output, _ = paths()
                    suffix = Path(uploaded.name).suffix.casefold()
                    destination = output / "recovered_sources" / f"{view.candidate_id}-{view.source_hash}{suffix}"
                    try:
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        destination.write_bytes(source_bytes)
                        review_store().record_source_relink(
                            candidate_id=view.candidate_id, source_hash=view.source_hash,
                            extraction_id=view.extraction_id, source_path=destination,
                            operation_id=f"ui-source-relink:{view.candidate_id}:{view.extraction_id}:{view.source_hash}",
                        )
                        st.success("The exact original bytes were restored and linked.")
                        st.rerun(scope="app")
                    except Exception as exc:
                        st.error(f"The source file was not linked. {exc}")
            _render_source_text(view, 1, output,
                                page_key=f"source_text_page:{view.candidate_id}",
                                allow_page_selection=True)
            _render_manual_approval(view, buffer)
            return
        count("preview_renders")
        page_key = f"page:{view.candidate_id}"
        range_first_page = view.range_provenance.get("first_page")
        initial_page = (int(range_first_page) if type(range_first_page) is int
                        else int(load_resume_pointer().get("source_page", 1)))
        st.session_state.setdefault(page_key, initial_page)
        page = int(st.session_state.get(page_key, 1))
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
        _render_source_text(view, page, output)
        with st.expander("Source and extraction details"):
            st.caption(f"Collection: {view.batch_name}")
            st.caption(f"Method: {view.candidate.source_method or 'Not recorded'}")
            if view.candidate.confidence:
                st.caption(f"Reported confidence: {view.candidate.confidence}")
            for note in view.candidate.uncertainties:
                st.write(note)


def _render_source_text(view, page_number: int, output_dir: Path,
                        *, page_key: str = "", allow_page_selection: bool = False) -> None:
    result = source_text_for_page(view, page_number, output_dir)
    if allow_page_selection and result.page_numbers:
        selected = st.session_state.get(page_key, page_number)
        if selected not in result.page_numbers:
            selected = result.page_numbers[0]
        st.session_state[page_key] = selected
        if len(result.page_numbers) > 1:
            st.selectbox("Source text page", result.page_numbers, key=page_key,
                         format_func=lambda value: f"{value} of {result.page_count}")
        result = source_text_for_page(view, int(st.session_state[page_key]), output_dir)

    st.subheader("Source text")
    if result.status == "available":
        st.caption(f"Read-only · page {result.page_number} of {result.page_count or result.page_number} · {result.method}")
        if not view.source_verified or not view.source_available:
            st.warning("Original source is unavailable. This cached text cannot be checked against the scan.")
        for note in result.uncertainty:
            if "Original source is unavailable" not in note:
                st.caption(note)
        st.text(result.text)
        return

    messages = {
        "identity_invalid": "Source text is unavailable because this recipe has no valid source identity.",
        "source_mismatch": "Source text is hidden because the selected file does not match this recipe's source hash.",
        "ambiguous_page": "Cached text has no reliable page mapping, so it is hidden rather than assigned to a page.",
        "cache_unreadable": "The saved source-text cache is missing, too large, or unreadable.",
        "page_unavailable": "That source page is unavailable.",
        "source_unavailable": "The original source cannot be checked and no safe cached text is available.",
        "no_text": "No selectable text or cached OCR text is available for this page.",
    }
    st.caption(f"Read-only · page {result.page_number} · {result.method or 'Source-derived text'}")
    st.info(messages.get(result.status, "Source-derived text is unavailable."))


@st.fragment
def _render_editor_fragment(view, buffer) -> None:
    diagnostics = (st.session_state.setdefault("_ui_diagnostics", new_counters())
                   if os.environ.get("RECIPE_IMPORTER_UI_DIAGNOSTICS") == "1" else None)
    bind_session(diagnostics)
    count("fragment_executions")
    # Tab and row widgets rerun this fragment only, so capture both sections
    # before Streamlit cleans up the inactive tab's widget state.
    sync_buffer(view, buffer)
    state = st.session_state.get("review_save_state")
    if isinstance(state, dict) and state.get("kind") == "draft_saved":
        if (state.get("candidate_id") != view.candidate_id
                or state.get("revision") != buffer.get("_revision")
                or state.get("signature") != _buffer_signature(view, buffer)
                or buffer_is_dirty(view, buffer)):
            st.session_state.pop("review_save_state", None)
        else:
            st.caption(state["message"])
    _render_editor(view, buffer)
    focus_request = st.session_state.pop("review_focus_request", None)
    if focus_request is not None:
        st.html(
            f"<!-- review focus request {int(focus_request)} -->"
            "<script>(() => { let attempts=0; const restore=() => { "
            "const target=document.querySelector('input[aria-label=\"Recipe title\"]'); "
            "if (target && document.activeElement !== target) target.focus({ preventScroll: true }); "
            "if (++attempts < 4 && document.activeElement !== target) requestAnimationFrame(restore); "
            "}; requestAnimationFrame(() => requestAnimationFrame(restore)); })();</script>",
            unsafe_allow_javascript=True,
        )
    if diagnostics is not None:
        with st.expander("Performance counters", expanded=True):
            st.caption(runtime_marker(st.context.url))
            st.json(dict(diagnostics))


def _render_editor(view, buffer) -> None:
    with st.container(border=True):
        st.markdown("**Recipe**")
        focus_field = st.session_state.pop("review_focus_field", None)
        if focus_field == "title":
            st.info("Review the Recipe title field below.")
        name_key = f"recipe_name:{view.candidate_id}"
        st.session_state.setdefault(name_key, str(buffer.get("name", view.candidate.name)))
        st.text_input("Recipe title", key=name_key, disabled=view.protected)
        if view.protected:
            st.text_area("Ingredients", "\n".join(view.candidate.ingredients), disabled=True, key=f"protected_i:{view.candidate_id}")
            st.text_area("Instructions", "\n\n".join(view.candidate.instructions), disabled=True, key=f"protected_s:{view.candidate_id}")
            blockers = _approval_blockers(view, view.candidate)
            if blockers:
                render_approval_blockers(
                    st, blockers,
                    on_jump=lambda anchor, code: _jump_to_blocker(anchor, view.candidate_id, code),
                )
            return
        with st.expander("Optional details", expanded=st.session_state.get("review_show_optional_details", False)):
            st.text_input("Yield", value=str(buffer.get("yield_text", "")),
                          key=f"recipe_yield_text:{view.candidate_id}")
            st.text_area("Description", value=str(buffer.get("description", "")), height=84,
                         key=f"recipe_description:{view.candidate_id}")
            st.text_area("Recipe notes and substitutions", value=str(buffer.get("recipe_notes", "")),
                         height=84, key=f"recipe_notes:{view.candidate_id}",
                         help="Kept separate from ingredient rows; notes are included in Mealie's recipe description.")
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
        if buffer_is_dirty(view, buffer):
            st.caption("Unsaved changes · use Save draft to keep these edits.")
        render_suggestion_summary(st, view.candidate.suggestions)
        candidate, _ = candidate_from_buffer(view, buffer)
        _render_no_instructions_control(view, buffer)
        candidate, _ = candidate_from_buffer(view, buffer)
        blockers = _approval_blockers(view, candidate)
        if blockers:
            render_approval_blockers(
                st, blockers,
                on_jump=lambda anchor, code: _jump_to_blocker(anchor, view.candidate_id, code),
            )


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


def _render_actions(view, buffer) -> None:
    with st.container(key="review-pane-switcher"):
        st.segmented_control(
            "Review pane", ["Source", "Recipe"], key="review_workspace_view",
        )
    with st.container(horizontal=True, vertical_alignment="center"):
        if not view.protected and st.button(
                "Save draft", key=f"save:{view.candidate_id}", width="stretch"):
            _save(view, buffer, advance=False)
        candidate, _warnings = candidate_from_buffer(view, buffer)
        blockers = _approval_blockers(view, candidate)
        if not view.protected and st.button(
                "Approve & next", key=f"approve:{view.candidate_id}",
                disabled=bool(blockers), type="primary", width="stretch"):
            _approve(view, buffer)
    if blockers:
        st.warning(f"Approval blocked: {blockers[0].label}", icon=":material/lock:")
    if not view.source_verified or not view.source_available:
        st.warning(
            "Original source unavailable. Review the current recipe carefully before using manual approval.",
            icon=":material/find_in_page:",
        )
    state = st.session_state.get("review_save_state")
    if isinstance(state, str) and state:
        st.caption(state)


def _render_navigation(view) -> None:
    skipped_count = len(_queue_state().skipped())
    cols = st.columns([1, 1.4, 1.8], vertical_alignment="center")
    cols[0].button("Back", key="review_back", on_click=_back_callback,
                   disabled=_queue_state().back(view.candidate_id) is None, width="stretch")
    cols[1].button("Skip for now", key=f"header_skip:{view.candidate_id}",
                   on_click=_queue_advance_callback, args=(1,), width="stretch")
    cols[2].button(f"Skipped recipes ({skipped_count})", key="open_skipped",
                   on_click=_open_skipped_callback, width="stretch")


def _render_structured_section(view, buffer, field: str) -> None:
    labels = {"ingredients": ("Ingredients", "ingredient", "ingredient_ids", "structured_ingredients", "structured_i"),
              "instructions": ("Instructions", "step", "instruction_ids", "structured_instructions", "structured_s")}
    label, kind, ids_key, value_key, widget_prefix = labels[field]
    st.markdown(f"**{label}**")
    if field == "instructions":
        _render_instruction_suggestion_request(view)
    _render_suggestions(view, buffer, field)
    for index, value in enumerate(list(buffer[value_key])):
        row_id = buffer[ids_key][index]
        target = st.session_state.get("review_quality_target")
        if (target == (view.candidate_id, f"{field}[{index}]")):
            st.warning("Selected extraction quality finding. Compare this row with the source scan.")
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
    for suggestion in view.candidate.suggestions:
        path = suggestion.field_path
        if path != field and not path.startswith(field + "["):
            continue
        current_bindings = (view.revision_target_bindings or view.target_bindings)
        binding = next((item for item in view.target_bindings
                        if item.get("issue_id") == suggestion.suggestion_id), None)
        target_id = getattr(suggestion, "target_id", None) or (binding.get("source_target_id") if binding else None)
        target_kind = binding.get("source_target_kind") if binding else ("row" if target_id else "field")
        row_ids = buffer.get("ingredient_ids" if field == "ingredients" else "instruction_ids", [])
        exact_row = target_kind == "row" and target_id in row_ids
        whole_field = path == field and target_kind == "field"
        if row_id is not None:
            if not exact_row or target_id != row_id:
                continue
        elif exact_row:
            continue
        can_apply = exact_row or whole_field or bool(getattr(suggestion, "generated", False))
        current = suggestion_current_value(
            suggestion, view.candidate, (*view.target_bindings, *current_bindings))
        with st.container(border=True):
            status = getattr(suggestion, "dynamic_status", None) or getattr(suggestion, "review_status", "pending")
            if not can_apply and status == "pending":
                st.caption("The original target is unavailable. Review this proposal in Issues; it cannot be applied by position.")
                continue
            action = render_suggestion_card(
                st, suggestion, current_value=current,
                key_prefix=f"inline:{view.candidate_id}:{field}:{row_id or 'section'}",
                can_apply=can_apply, can_rebase=not buffer_is_dirty(view, buffer),
                critical_finding=any(
                    finding.severity == "critical" and finding.field_path == path
                    for finding in view.critical_findings
                ), buffer=buffer,
            )
        if action is not None:
            disposition = action.disposition
            if getattr(suggestion, "generated", False) and disposition == "rejected":
                disposition = "declined"
            if disposition == "rebased":
                _run_issue_action(lambda: _rebase_dynamic_suggestion(
                    view, suggestion, action.final_value, buffer))
            else:
                _run_issue_action(lambda: _apply_proposal(view, suggestion, disposition, action.final_value, buffer))


def _rebase_dynamic_suggestion(view, suggestion, final_value: str | None, buffer) -> None:
    if not getattr(suggestion, "generated", False):
        raise ValueError("Only a generated suggestion can be compared to a newer recipe revision.")
    if buffer_is_dirty(view, buffer):
        raise ValueError("Save the current recipe edits before comparing this suggestion.")
    target_id = str(getattr(suggestion, "target_id", "") or "")
    insertion_location = None
    if target_id:
        target = next((item for item in view.revision_target_bindings
                       if item.get("target_id") == target_id
                       and item.get("field") == "instructions"
                       and isinstance(item.get("position"), int)), None)
        if target is None:
            raise ValueError("The original instruction step is no longer available; discard this suggestion instead.")
        position = int(target["position"])
        values = buffer.setdefault("structured_instructions", [])
        ids = buffer.setdefault("instruction_ids", [])
        if position >= len(values) or position >= len(ids) or ids[position] != target_id:
            raise ValueError("The instruction step moved or disappeared; compare it again before applying.")
        values[position] = str(final_value or "")
    else:
        values = buffer.setdefault("structured_instructions", [])
        ids = buffer.setdefault("instruction_ids", [])
        insertion_location = {"field": "instructions", "index": len(values)}
        values.append(str(final_value or ""))
        ids.append(str(uuid.uuid4()))
    _update_raw_section(buffer, "instructions")
    candidate, _warnings = candidate_from_buffer(view, buffer)
    review_store().rebase_and_apply_dynamic_suggestion(
        candidate_id=view.candidate_id,
        proposal_id=str(getattr(suggestion, "suggestion_id", "")),
        expected_parent=view.revision,
        expected_content_digest=view.content_digest,
        recipe=candidate.model_dump(mode="json"),
        target_bindings=_revision_bindings(view, buffer, candidate),
        disposition="accepted", final_value=str(final_value or ""),
        source_path=view.source_path, confirmed=True,
        insertion_location=insertion_location,
        operation_id=f"ui-dynamic-rebase:{view.candidate_id}:{getattr(suggestion, 'suggestion_id', '')}:{uuid.uuid4().hex}",
    )
    st.session_state.review_refresh_candidate = view.candidate_id


def _render_instruction_suggestion_request(view) -> None:
    _render_suggestion_request_controls(view)


def _render_suggestion_job_status(view) -> None:
    request = _suggestion_request_snapshot(view)
    if not request:
        return
    if request.get("status") in {"queued", "running"}:
        _poll_suggestion_job_status(view)
        return
    _render_suggestion_job_status_content(view, request)


@st.fragment(run_every="2s")
def _poll_suggestion_job_status(view) -> None:
    request = _suggestion_request_snapshot(view)
    if not request or request.get("status") not in {"queued", "running"}:
        # Drop the periodic fragment as soon as the durable worker is terminal.
        st.rerun(scope="app")
        return
    _render_suggestion_job_status_content(view, request)


def _render_suggestion_job_status_content(view, request: dict) -> None:
    candidate_id = view.candidate_id
    request_id = request["request_id"]
    status = request.get("status", "queued")
    labels = {"queued": "AI suggestion request is waiting to start.",
              "running": "AI is preparing unverified suggestions. You can continue editing.",
              "completed": "AI suggestions are ready for review.",
              "failed": "The suggestion request failed. Your recipe is unchanged.",
              "quota_blocked": "Gemini quota is unavailable. Your recipe is unchanged.",
              "interrupted": "The request stopped before completion. Start a new request when ready.",
              "cancelled": "The suggestion request was cancelled.",
              "stale": "The recipe changed while suggestions were generated; compare before using them."}
    if status in {"queued", "running"}:
        st.info(labels.get(status, "AI suggestion request is in progress."))
        actions = st.columns(2)
        if status == "queued" and actions[0].button(
                "Resume queued request", key=f"resume_suggestion:{request_id}"):
            try:
                suggestion_service().resume(request_id)
                _invalidate_suggestion_request_snapshot(candidate_id)
                st.rerun(scope="app")
            except Exception as exc:
                st.error(f"The queued request could not be resumed. {exc}")
        if actions[1].button("Cancel request", key=f"cancel_suggestion:{request_id}"):
            try:
                suggestion_service().cancel(request_id)
                _invalidate_suggestion_request_snapshot(candidate_id)
                st.rerun(scope="app")
            except Exception as exc:
                st.error(f"The request could not be cancelled. {exc}")
    elif status == "completed":
        st.success(labels[status])
    elif status in {"failed", "quota_blocked", "interrupted", "cancelled", "stale"}:
        st.warning(labels.get(status, "The suggestion request did not complete."))
        if request.get("error"):
            st.caption(request["error"])


def _render_suggestion_request_controls(view) -> None:
    if view.protected:
        st.caption("AI suggestions are unavailable for protected recipes.")
        return
    from runtime_safety import fixture_mode
    request = _suggestion_request_snapshot(view)
    active = bool(request and request.get("status") in {"queued", "running"})
    from gemini_config import gemini_api_key
    api_available = bool(gemini_api_key())
    fixture_disabled = fixture_mode()
    identity_ready = view.identity_status == "registered"
    enough_information = bool(view.candidate.name.strip()) and any(
        str(item or "").strip() for item in view.candidate.ingredients
    )
    has_usable_instructions = any(
        str(item or "").strip() for item in view.candidate.instructions
    )
    label = ("Suggest missing instructions with AI" if not has_usable_instructions
             else "Suggest improvements with AI")
    with st.container(border=True):
        st.markdown("**AI-generated instructions are inferred and unverified.**")
        st.caption("Suggestions will be inferred from recipe text, not transcribed from the scan. This sends only the recipe title, ingredients, and current instructions to Google Gemini as text; no source image or PDF is sent.")
        if not view.source_available:
            st.warning(
                "Original scan unavailable. Gemini can suggest instructions using the recipe text, "
                "but those suggestions remain unverified. Restore the exact source scan, or review "
                "the current recipe and use the explicit manual approval path if you accept that it "
                "cannot be checked against the scan."
            )
        consent_nonce_key = f"suggestion_consent_nonce:{view.candidate_id}"
        st.session_state.setdefault(consent_nonce_key, uuid.uuid4().hex)
        consent_nonce = st.session_state[consent_nonce_key]
        consent_key = f"suggestion_consent:{view.candidate_id}:{view.revision}:{consent_nonce}"
        consent = st.checkbox(
            "I authorize sending this recipe's title, ingredients, and current instructions to Google Gemini.",
            key=consent_key,
            disabled=active or not api_available or fixture_disabled or not identity_ready or not enough_information,
            persist_state="page",
        )
        if not api_available:
            st.caption("Gemini is not configured. Add its API key before requesting suggestions.")
        if active:
            st.caption("Another suggestion request is already running for this recipe.")
        if not identity_ready:
            st.caption("Recipe identity requires reconciliation before requesting suggestions.")
        if not enough_information:
            st.caption("Add a recipe title and at least one ingredient before requesting suggestions.")
        if fixture_disabled:
            st.caption("Fixture mode disables live Gemini requests.")
        if st.button(label, key=f"request_instructions:{view.candidate_id}:{view.revision}:{consent_nonce}",
                     disabled=(active or not api_available or fixture_disabled or not identity_ready
                               or not enough_information or not consent),
                     type="primary"):
            st.session_state[consent_nonce_key] = uuid.uuid4().hex
            try:
                operation_id = f"ui-instruction-request:{view.candidate_id}:{view.revision}:{uuid.uuid4().hex}"
                result = enqueue_suggestion_request(view, operation_id=operation_id)
                st.session_state[f"suggestion_request:{view.candidate_id}"] = result["request_id"]
                _invalidate_suggestion_request_snapshot(view.candidate_id)
                st.rerun()
            except Exception as exc:
                st.error(f"The suggestion request was not started. Your recipe remains unchanged. {exc}")


def _render_findings(view, buffer) -> None:
    active_statuses = {"pending", "stale", "target_missing", "rejected"}
    unresolved_suggestions = [item for item in view.candidate.suggestions
                              if (getattr(item, "dynamic_status", None)
                                  or getattr(item, "review_status", "pending")) in active_statuses]
    issue_count = len(view.critical_findings) + len(unresolved_suggestions)
    with st.expander(f"Issues and source notes ? {issue_count} unresolved",
                     expanded=bool(st.session_state.pop("review_show_issues", False)) or issue_count > 0):
        if view.warnings:
            st.markdown("**Source and OCR notes**")
            for warning in view.warnings:
                st.warning(warning)
        for index, finding in enumerate(view.critical_findings):
            with st.container(border=True):
                if st.session_state.get("review_finding_target") == (view.candidate_id, finding.field_path):
                    st.info("Review this finding")
                    st.session_state.pop("review_finding_target", None)
                st.warning(f"{finding.field_path}: {finding.detail}")
                if finding.source_evidence:
                    st.caption(f"Source evidence: {finding.source_evidence}")
                st.caption(f"Next: {finding.suggested_action}")
                resolution = render_critical_finding_resolution(
                    st, finding, key_prefix=f"{view.candidate_id}:{index}:{finding.field_path}")
                if resolution and resolution != "deferred":
                    evidence = ("Operator verified the field against the original source scan."
                                if resolution == "source_corrected"
                                else "Operator explicitly accepted the current wording as inferred recipe content.")
                    _run_issue_action(lambda: _resolve_finding(
                        view, buffer, finding, index, resolution, evidence))
        for suggestion in view.candidate.suggestions:
            status = getattr(suggestion, "dynamic_status", None) or getattr(suggestion, "review_status", "pending")
            path = suggestion.field_path
            current_bindings = (view.revision_target_bindings or view.target_bindings)
            binding = next((item for item in view.target_bindings
                            if item.get("issue_id") == suggestion.suggestion_id), None)
            target_id = getattr(suggestion, "target_id", None)
            mapped = bool(getattr(suggestion, "generated", False) or
                          (binding and (binding.get("source_target_id") or binding.get("target_id"))) or
                          (target_id and any(item.get("target_id") == target_id
                                             for item in current_bindings)))
            current = suggestion_current_value(suggestion, view.candidate, current_bindings)
            if path in {"ingredients", "instructions"} or path.startswith(("ingredients[", "instructions[")):
                if status not in active_statuses:
                    continue
                if status in {"pending", "stale"} and mapped:
                    # The mapped proposal and its decisions are shown inline by the field editor.
                    continue
            with st.container(border=True):
                if st.session_state.get("review_suggestion_target") == (
                        view.candidate_id, getattr(suggestion, "suggestion_id", "")):
                    st.info("Review this suggestion")
                    st.session_state.pop("review_suggestion_target", None)
                st.markdown(f"**{path} ? {suggestion_status_label(status)}**")
                st.caption("Source-supported proposal" if getattr(suggestion, "classification", "inferred") == "source_supported"
                           else "AI-inferred completion ? unverified")
                if current:
                    st.markdown("**Current recipe wording**")
                    st.write(current)
                st.markdown("**Proposed wording**")
                st.write(suggestion.proposed_value)
                st.caption(f"Why: {suggestion.rationale} ? Uncertainty: {suggestion.uncertainty or 'Not stated'}")
                if status == "pending":
                    action = render_suggestion_card(
                        st, suggestion, current_value=current,
                        key_prefix=f"issues:{view.candidate_id}", can_apply=mapped or getattr(suggestion, "generated", False),
                        critical_finding=any(item.severity == "critical" and item.field_path == path
                                             for item in view.critical_findings), buffer=buffer,
                    )
                    if action is not None:
                        disposition = action.disposition
                        if getattr(suggestion, "generated", False) and disposition == "rejected":
                            disposition = "declined"
                        _run_issue_action(lambda: _apply_proposal(
                            view, suggestion, disposition, action.final_value, buffer))
                elif status == "rejected":
                    st.info("Rejecting this proposal leaves the source issue unresolved. Confirm a separate follow-up if appropriate.")
                    verified = st.checkbox(
                        "I verified the current wording against the source scan",
                        key=f"keep_original_verified:{view.candidate_id}:{suggestion.suggestion_id}",
                    )
                    if st.button("Keep verified original value",
                                 key=f"keep_current:{view.candidate_id}:{suggestion.suggestion_id}",
                                 disabled=not verified or not current):
                        _run_issue_action(lambda: _resolve_suggestion(
                            view, suggestion, "source_corrected", current, buffer,
                            "Operator checked the current value against the original source scan."))
                    evidence_key = f"omission_evidence:{view.candidate_id}:{suggestion.suggestion_id}"
                    evidence = st.text_input("Evidence for justified omission", key=evidence_key)
                    if st.button("Record justified omission",
                                 key=f"omit_s:{view.candidate_id}:{suggestion.suggestion_id}",
                                 disabled=not evidence.strip()):
                        _run_issue_action(lambda: _resolve_suggestion(
                            view, suggestion, "not_recipe_content", None, buffer, evidence.strip()))
                    if st.button("Defer review", key=f"defer_s:{view.candidate_id}:{suggestion.suggestion_id}"):
                        _run_issue_action(lambda: _resolve_suggestion(
                            view, suggestion, "deferred", None, buffer, "Operator deferred this proposal."))
                elif status in {"stale", "target_missing"}:
                    st.warning("This proposal cannot be applied to the current recipe. Compare it with the current wording or request a new suggestion for this revision.")
    if buffer.get("_undo"):
        if st.button("Undo last edit", key=f"undo:{view.candidate_id}"):
            undo = buffer.pop("_undo")
            buffer.update(undo)
            try:
                record, _ = save_draft(view, buffer)
                st.session_state.review_save_state = f"Undo saved locally ? revision {record.revision}"
            except Exception as exc:
                st.error(f"Undo could not be saved; your current buffer is retained. {exc}")
                buffer["_undo"] = undo
                return
            st.rerun()


def _render_ingredient_repair(view, buffer) -> None:
    candidate, _ = candidate_from_buffer(view, buffer)
    findings = validate_recipe(candidate)
    if not findings:
        return
    st.warning("Some ingredient entries may contain preparation instructions or substitution notes.")
    for finding in findings:
        with st.expander(f"{finding.field_path}: {finding.category.replace('_', ' ')}"):
            st.write(finding.explanation)
            st.code(finding.original_text)
            if finding.source_references:
                refs = ", ".join(f"lines {item.get('start')}-{item.get('end')}"
                                  for item in finding.source_references)
                st.caption(f"Source reference: {refs}. Verify the wording against the scan.")
            st.caption(finding.suggested_action)
    key = f"ingredient_repair_open:{view.candidate_id}"
    if not st.session_state.get(key):
        if st.button("Review ingredient extraction", key=f"open_repair:{view.candidate_id}",
                     disabled=view.protected):
            st.session_state[key] = True
            st.rerun()
        return
    stored = dict(buffer.get("repair_proposal", {}) or {})
    valid_stored = (stored.get("source_hash") == view.source_hash
                    and stored.get("extraction_id") == view.extraction_id
                    and int(stored.get("base_revision", -1)) == view.revision
                    and isinstance(stored.get("proposal"), dict))
    proposed = stored.get("proposal") if valid_stored else propose_repair(candidate).__dict__
    if not view.source_verified:
        st.info("Inferred reorganization from the saved extraction text. The original scan is unavailable or not verified.")
    else:
        st.info("Suggested reorganization from the saved extraction. Check each proposed value against the visible scan.")
    with st.form(f"repair_form:{view.candidate_id}"):
        ingredients_text = st.text_area(
            "Proposed ingredients", value="\n".join(proposed.get("ingredients", [])), height=180,
            key=f"repair_ingredients:{view.candidate_id}")
        instructions_text = st.text_area(
            "Proposed instructions", value="\n\n".join(proposed.get("instructions", [])), height=180,
            key=f"repair_instructions:{view.candidate_id}")
        notes_text = st.text_area(
            "Proposed notes and substitutions", value="\n".join(proposed.get("notes", [])), height=100,
            key=f"repair_notes:{view.candidate_id}")
        for uncertainty in proposed.get("uncertainties", []):
            st.warning(uncertainty)
        left, right, cancel = st.columns(3)
        save_proposal = left.form_submit_button("Save repair draft", width="stretch")
        apply_proposal = right.form_submit_button("Apply proposed repair", type="primary", width="stretch")
        cancel_proposal = cancel.form_submit_button("Cancel", width="stretch")
    if cancel_proposal:
        save_repair_proposal(view, buffer, {})
        st.session_state.pop(key, None)
        st.rerun()
    if save_proposal or apply_proposal:
        edited = {
            "ingredients": [line.strip() for line in ingredients_text.splitlines() if line.strip()],
            "instructions": [part.strip() for part in instructions_text.split("\n\n") if part.strip()],
            "notes": [line.strip() for line in notes_text.splitlines() if line.strip()],
            "uncertainties": list(proposed.get("uncertainties", [])),
            "inferred": True,
        }
        record = {"source_hash": view.source_hash, "extraction_id": view.extraction_id,
                  "base_revision": view.revision, "proposal": edited}
        if save_proposal:
            save_repair_proposal(view, buffer, record)
            st.session_state.review_save_state = "Repair proposal saved locally. The recipe has not changed."
            st.rerun()
        if apply_proposal:
            blockers = (view.protected or view.identity_status != "registered"
                        or bool(view.candidate.suggestions) or bool(view.critical_findings))
            if blockers:
                st.error("This recipe has protected status or existing review decisions that need reconciliation before a structural repair can be applied.")
                return
            prior_buffer = {
                name: (list(value) if isinstance(value, list) else value)
                for name, value in buffer.items()
                if name in {"structured_ingredients", "structured_instructions", "ingredient_ids",
                            "instruction_ids", "recipe_notes", "raw_ingredients", "raw_instructions",
                            "mode", "_revision", "_saved_signature"}
            }
            old_instructions = prior_buffer.get("structured_instructions", [])
            old_instruction_ids = prior_buffer.get("instruction_ids", [])
            buffer["structured_ingredients"] = edited["ingredients"]
            buffer["structured_instructions"] = edited["instructions"]
            buffer["ingredient_ids"] = [str(uuid.uuid4()) for _ in edited["ingredients"]]
            buffer["instruction_ids"] = [
                old_instruction_ids[old_instructions.index(item)]
                if item in old_instructions and old_instructions.index(item) < len(old_instruction_ids)
                else str(uuid.uuid4()) for item in edited["instructions"]
            ]
            buffer["recipe_notes"] = "\n".join(edited["notes"])
            buffer["mode"] = "structured"
            _update_raw_section(buffer, "ingredients")
            _update_raw_section(buffer, "instructions")
            try:
                saved, _ = save_draft(view, buffer)
                save_repair_proposal(view, buffer, {})
                st.session_state.review_save_state = f"Repair saved as review revision {saved.revision}. Review it before approval."
                st.session_state.pop(key, None)
                st.rerun()
            except Exception as exc:
                buffer.update(prior_buffer)
                st.error(f"The repair was not saved. The original recipe revision remains intact. {exc}")
    if st.button("Keep original and defer", key=f"defer_repair:{view.candidate_id}"):
        save_repair_proposal(view, buffer, {
            "source_hash": view.source_hash, "extraction_id": view.extraction_id,
            "base_revision": view.revision, "status": "deferred",
        })
        st.session_state.pop(key, None)
        st.rerun()


def _current_value_for_path(candidate, field_path: str) -> str | None:
    import re
    match = re.fullmatch(r"(ingredients|instructions)\[(\d+)\]", field_path)
    if match:
        values = getattr(candidate, match.group(1))
        index = int(match.group(2))
        return values[index] if index < len(values) else None
    if field_path == "yield_text":
        return candidate.yield_text or ""
    if field_path == "description":
        return candidate.description or ""
    return None



main()
