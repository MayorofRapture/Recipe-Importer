"""Presentation helpers for proposal decisions and approval blockers.

This module owns no review authority. Callers persist decisions through ReviewStore
or SuggestionService and render this projection from the effective recipe view.
"""
from __future__ import annotations

from dataclasses import dataclass
from difflib import ndiff
from typing import Any, Callable, Iterable


_STATUS_LABELS = {
    "pending": "Awaiting review",
    "approved": "Applied",
    "accepted": "Applied",
    "edited": "Applied with edits",
    "declined": "Declined \u2014 no change required",
    "rejected": "Rejected \u2014 source issue still unresolved",
    "stale": "Stale \u2014 recipe changed",
    "target_missing": "Could not apply \u2014 target missing",
}


@dataclass(frozen=True)
class ApprovalBlocker:
    code: str
    label: str
    anchor: str


def suggestion_status_label(status: str | None) -> str:
    """Translate durable proposal states into plain-language review labels."""
    value = str(status or "pending").casefold()
    return _STATUS_LABELS.get(value, "Awaiting review" if value == "" else value.replace("_", " ").capitalize())


def _display_field_path(field_path: str) -> str:
    """Use familiar recipe labels in cards while retaining exact paths in the ledger."""
    for prefix, label in (("instructions[", "Step"), ("ingredients[", "Ingredient")):
        if field_path.startswith(prefix) and field_path.endswith("]"):
            try:
                return f"{label} {int(field_path[len(prefix):-1]) + 1}"
            except ValueError:
                break
    labels = {"instructions": "Instructions", "ingredients": "Ingredients",
              "yield_text": "Yield", "prep_time": "Prep time", "cook_time": "Cook time"}
    return labels.get(field_path, field_path.replace("_", " ").capitalize())


def _provenance_label(classification: str, status: str) -> str:
    if classification == "source_supported":
        return "Source-supported proposal"
    if status in {"accepted", "approved"}:
        return "AI-inferred completion · human-approved"
    if status == "edited":
        return "AI-inferred completion · human-edited"
    return "AI-inferred completion · unverified"


def _status(suggestion: Any) -> str:
    return str(getattr(suggestion, "dynamic_status", None)
               or getattr(suggestion, "review_status", None)
               or getattr(suggestion, "status", None) or "pending")


def suggestion_summary(suggestions: Iterable[Any]) -> str:
    """Build a compact, truthful count of proposal states, including completed ones."""
    counts: dict[str, int] = {}
    for suggestion in suggestions:
        status = _status(suggestion).casefold()
        counts[status] = counts.get(status, 0) + 1
    parts: list[str] = []
    if counts.get("pending"):
        parts.append(f"{counts['pending']} awaiting review")
    if counts.get("approved", 0) + counts.get("accepted", 0):
        parts.append(f"{counts.get('approved', 0) + counts.get('accepted', 0)} applied")
    if counts.get("edited"):
        parts.append(f"{counts['edited']} applied with edits")
    if counts.get("declined"):
        parts.append(f"{counts['declined']} declined")
    if counts.get("rejected"):
        parts.append(f"{counts['rejected']} rejected; source issue unresolved")
    if counts.get("stale"):
        parts.append(f"{counts['stale']} stale")
    if counts.get("target_missing"):
        parts.append(f"{counts['target_missing']} could not apply")
    return "Suggestions: " + (" \u00b7 ".join(parts) if parts else "none")


def approval_blockers(view: Any, candidate: Any | None = None, *,
                      suggestion_request_status: str | None = None) -> list[ApprovalBlocker]:
    """Return the concrete reasons approval is blocked in the order to resolve them."""
    candidate = candidate or view.candidate
    blockers: list[ApprovalBlocker] = []
    if view.protected:
        blockers.append(ApprovalBlocker("protected", "Resolve Mealie reconciliation", "recipe-identity"))
    if view.identity_status != "registered":
        blockers.append(ApprovalBlocker("identity", "Recipe identity requires reconciliation", "recipe-identity"))
    if not view.source_available:
        blockers.append(ApprovalBlocker("source", "Original source unavailable", "source-preview"))
    if view.legacy_draft_conflict:
        blockers.append(ApprovalBlocker("draft", "Save the current revision before approval", "recipe-editor"))
    if suggestion_request_status in {"queued", "running"}:
        blockers.append(ApprovalBlocker(
            "suggestion_request", "AI suggestion request in progress", "suggestion-request"))
    missing = []
    if not str(candidate.name or "").strip():
        missing.append("recipe title")
    if not any(str(item).strip() for item in candidate.ingredients):
        missing.append("ingredients")
    if not any(str(item).strip() for item in candidate.instructions):
        missing.append("recipe instructions")
    for field in missing:
        label = "Missing " + field
        blockers.append(ApprovalBlocker("incomplete", label, "recipe-editor"))
    critical_count = len(view.critical_findings)
    if critical_count:
        suffix = "s" if critical_count != 1 else ""
        blockers.append(ApprovalBlocker(
            "critical", f"{critical_count} critical source issue{suffix} unresolved", "critical-findings"))
    unresolved = [item for item in getattr(candidate, "suggestions", [])
                  if _status(item).casefold() in {"pending", "stale", "target_missing", "rejected"}]
    pending_count = sum(_status(item).casefold() == "pending" for item in unresolved)
    if pending_count:
        suffix = "s" if pending_count != 1 else ""
        blockers.append(ApprovalBlocker(
            "suggestions", f"{pending_count} suggestion{suffix} awaiting review", "suggestions"))
    stale_count = sum(_status(item).casefold() == "stale" for item in unresolved)
    if stale_count:
        suffix = "s" if stale_count != 1 else ""
        blockers.append(ApprovalBlocker(
            "stale", f"{stale_count} suggestion{suffix} need comparison with the current recipe", "suggestions"))
    missing_target_count = sum(_status(item).casefold() == "target_missing" for item in unresolved)
    if missing_target_count:
        suffix = "s" if missing_target_count != 1 else ""
        blockers.append(ApprovalBlocker(
            "target_missing", f"{missing_target_count} suggestion target{suffix} could not be found", "suggestions"))
    rejected_count = sum(_status(item).casefold() == "rejected" for item in unresolved)
    if rejected_count:
        suffix = "s" if rejected_count != 1 else ""
        blockers.append(ApprovalBlocker(
            "rejected", f"{rejected_count} rejected suggestion{suffix} still have an unresolved source issue", "critical-findings"))
    return blockers


def suggestion_current_value(suggestion: Any, candidate: Any,
                            target_bindings: Iterable[dict[str, Any]]) -> str:
    """Resolve the current wording through immutable target IDs, never array order alone."""
    target_id = getattr(suggestion, "target_id", None)
    issue_id = getattr(suggestion, "suggestion_id", None) or getattr(suggestion, "proposal_id", None)
    bindings = list(target_bindings)
    if not target_id:
        issue_binding = next((item for item in bindings if item.get("issue_id") == issue_id), None)
        target_id = issue_binding.get("source_target_id") if issue_binding else None
    target = next((item for item in bindings if item.get("target_id") == target_id
                   and item.get("field") in {"ingredients", "instructions"}
                   and isinstance(item.get("position"), int)), None)
    if not target:
        return ""
    field = str(target.get("field", ""))
    position = target.get("position")
    values = getattr(candidate, field, None)
    if not isinstance(values, list) or not isinstance(position, int) or not 0 <= position < len(values):
        return ""
    return str(values[position])


def suggestion_diff(current: str, proposed: str) -> str:
    """Return a compact, escaped-by-renderer text diff for inline inspection."""
    if not current:
        return "+ " + proposed
    if current == proposed:
        return "No wording change."
    return "\n".join(["- " + current, "+ " + proposed])


def render_suggestion_summary(st: Any, suggestions: Iterable[Any]) -> None:
    """Render the compact status ledger from an already-projected suggestion list."""
    st.caption(suggestion_summary(suggestions))


def render_approval_blockers(st: Any, blockers: list[ApprovalBlocker], on_jump: Callable[[str], None] | None = None) -> None:
    """Show actual blockers with an optional direct jump control for each one."""
    for index, blocker in enumerate(blockers):
        if blocker.anchor == "recipe-identity":
            st.warning(blocker.label, icon=":material/error:")
            if on_jump and st.button("Resolve identity", key=f"approval_blocker:{blocker.code}:{index}",
                                     help="Open the source-backed identity comparison"):
                on_jump(blocker.anchor)
            continue
        columns = st.columns([5, 1], vertical_alignment="center")
        columns[0].warning(blocker.label, icon=":material/error:")
        if on_jump and columns[1].button("Review", key=f"approval_blocker:{blocker.code}:{index}",
                                         help=f"Go to {blocker.label.casefold()}"):
            on_jump(blocker.anchor)


@dataclass(frozen=True)
class SuggestionAction:
    disposition: str
    final_value: str | None = None


def render_critical_finding_resolution(st: Any, finding: Any, *, key_prefix: str) -> str | None:
    """Offer explicit, finding-scoped resolution; proposal decisions never call this implicitly."""
    kind = str(getattr(finding, "kind", ""))
    field_path = str(getattr(finding, "field_path", "critical source issue"))
    with st.container(border=True):
        st.markdown(f"**Resolve this source issue ? {field_path}**")
        st.write(getattr(finding, "detail", "Review the original source before resolving."))
        if kind in {"unreadable", "cropped", "ambiguous", "not_extracted"}:
            verified = st.checkbox(
                "I checked the source and corrected this field",
                key=f"confirm_source_resolution:{key_prefix}",
            )
            if st.button("Record source correction", key=f"source_resolution:{key_prefix}", disabled=not verified):
                return "source_corrected"
        elif kind == "missing_from_source":
            accepted = st.checkbox(
                "I accept this as a human-approved inferred recipe completion",
                key=f"confirm_completion_resolution:{key_prefix}",
            )
            if st.button("Accept completion and clear this issue", key=f"completion_resolution:{key_prefix}", disabled=not accepted):
                return "human_completion"
        if st.button("Defer review ? keep issue open", key=f"defer_resolution:{key_prefix}"):
            return "deferred"
    return None


def render_suggestion_card(st: Any, suggestion: Any, *, current_value: str = "",
                           key_prefix: str, can_apply: bool = True,
                           can_rebase: bool = True,
                           critical_finding: bool = False,
                           buffer: dict[str, Any] | None = None) -> SuggestionAction | None:
    """Render one inline proposal and return an explicit user decision for the caller to persist."""
    status = _status(suggestion).casefold()
    proposal_id = str(getattr(suggestion, "suggestion_id", None) or getattr(suggestion, "proposal_id", "proposal"))
    classification = str(getattr(suggestion, "classification", "inferred"))
    provenance = _provenance_label(classification, status)
    field_path = str(getattr(suggestion, "field_path", "recipe field"))
    proposed = str(getattr(suggestion, "proposed_value", ""))
    with st.container(border=True):
        st.markdown(f"**{_display_field_path(field_path)} \u00b7 {suggestion_status_label(status)}**")
        st.caption(provenance)
        if current_value:
            st.markdown("**Current recipe wording**")
            st.write(current_value)
        st.markdown("**Proposed wording**")
        st.write(proposed)
        st.caption("What changed")
        st.text(suggestion_diff(current_value, proposed))
        st.caption(f"Why: {getattr(suggestion, 'rationale', '') or 'Not stated'} \u00b7 Uncertainty: {getattr(suggestion, 'uncertainty', '') or 'Not stated'}")
        if getattr(suggestion, "source_evidence", ""):
            st.caption(f"Source evidence: {suggestion.source_evidence}")
        if status == "stale":
            st.warning("This proposal was made for an earlier recipe version. Compare both values before discarding it; it has not been applied.")
            target_id = str(getattr(suggestion, "target_id", "") or "")
            insertion = getattr(suggestion, "insertion_location", {}) or {}
            can_rebase_target = bool(can_rebase and getattr(suggestion, "generated", False)
                                     and target_id and current_value)
            can_rebase_insertion = bool(can_rebase and getattr(suggestion, "generated", False)
                                        and not target_id
                                        and insertion.get("field") == "instructions")
            confirmation_label = (
                "I compared this instruction and approve appending it after the current steps"
                if can_rebase_insertion else
                "I compared the current recipe wording with this proposal"
            )
            reviewed = st.checkbox(confirmation_label,
                                   key=f"compare_stale:{key_prefix}:{proposal_id}")
            if st.button("Discard stale suggestion", key=f"discard_stale:{key_prefix}:{proposal_id}",
                         disabled=not reviewed):
                return SuggestionAction("discard_stale")
            if can_rebase_target or can_rebase_insertion:
                label = "Append after current steps" if can_rebase_insertion else "Use with current revision"
                if st.button(label, key=f"rebase_stale:{key_prefix}:{proposal_id}",
                             disabled=not reviewed):
                    return SuggestionAction("rebased", proposed)
            elif getattr(suggestion, "generated", False) and buffer is not None:
                st.info("Save current recipe edits before comparing or applying this older proposal.")
            return None
        if status != "pending":
            return None
        if not can_apply:
            st.info("The original target is unavailable. Review the source and recipe before deciding.")
            return None
        if critical_finding:
            st.info("This proposal decision will not clear the critical source issue. Resolve that exact issue separately below.")
        source_verified = False
        if classification == "source_supported":
            source_verified = st.checkbox(
                "I verified this correction against the source scan",
                key=f"source_verify:{key_prefix}:{proposal_id}",
            )
        edit_key = f"edit_mode:{key_prefix}:{proposal_id}"
        if st.session_state.get(edit_key):
            value_key = f"edit_value:{key_prefix}:{proposal_id}"
            if buffer is not None:
                st.session_state.setdefault(value_key, buffer.get("_suggestion_edits", {}).get(proposal_id, proposed))
                value = st.text_area("Edit and use", key=value_key)
                buffer.setdefault("_suggestion_edits", {})[proposal_id] = value
            else:
                value = st.text_area("Edit and use", value=proposed, key=value_key)
            col_a, col_b = st.columns(2)
            if col_a.button("Use edited version", key=f"use_edited:{key_prefix}:{proposal_id}",
                             disabled=not bool(value.strip()) or (classification == "source_supported" and not source_verified)):
                return SuggestionAction("edited", value.strip())
            if col_b.button("Cancel edit", key=f"cancel_edit:{key_prefix}:{proposal_id}"):
                st.session_state.pop(edit_key, None)
                st.rerun()
        else:
            columns = st.columns(3)
            apply_label = "Use suggestion" if classification == "source_supported" else "Accept inferred suggestion"
            if columns[0].button(apply_label, key=f"use:{key_prefix}:{proposal_id}",
                                 disabled=classification == "source_supported" and not source_verified):
                return SuggestionAction("accepted", proposed)
            if columns[1].button("Edit and use", key=f"edit:{key_prefix}:{proposal_id}"):
                st.session_state[edit_key] = True
                st.rerun()
            if columns[2].button("Decline" if not critical_finding else "Reject; keep issue open",
                                 key=f"decline:{key_prefix}:{proposal_id}"):
                return SuggestionAction("declined" if not critical_finding else "rejected")
    return None
