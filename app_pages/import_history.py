"""Selected exact-revision imports and independent durable history."""
import math
import uuid
import streamlit as st

from app_pages.common import catalog, paths
from application_services import configured_mealie_client
from runtime_safety import fixture_mode
from ui_import_service import ImportService
from ui_data import human_source_name


def connected_service():
    output, sources = paths()
    return ImportService(output, sources, client=configured_mealie_client(output))


def failure(action, error):
    st.error(f"{action} could not finish. Your history is preserved. Check configuration and local records before retrying. ({type(error).__name__})")


def _reconciliation_next_step(status: str) -> str:
    return {
        "confirmation_required": "Compare every available field, record evidence, and acknowledge any differences or unavailable fields.",
        "linked_existing": "No retry is needed; this Mealie recipe is durably linked to the protected attempt.",
        "already_linked": "No retry is needed; the existing Mealie recipe is already linked.",
        "not_proven_absent": "Keep the recipe protected. Check Mealie directly; a lookup miss does not authorize retry.",
    }.get(status, "Keep the recipe protected. Check the local history and Mealie identity before taking another action.")


def _persist_reconciliation_result(service, result: dict, *, candidate_id: str,
                                   action_id: str, action: str, attempt_id: str,
                                   slug: str) -> dict:
    status = str(result.get("creation_status", "not_verified"))
    reason = str(result.get("reason", ""))
    service.record_reconciliation_feedback(
        candidate_id=candidate_id, action_id=action_id, action=action,
        result_status=status, reason=reason, attempt_id=attempt_id, slug=slug,
        next_step=_reconciliation_next_step(status), protection_active=True,
    )
    # The UI action owns these values even when a service error omits its identifiers.
    result.update({"candidate_id": candidate_id, "action_id": action_id,
                   "attempt_id": attempt_id, "slug": slug})
    return result


st.title("Import & history")
st.caption("Choose approved recipes to send to Mealie. Approval and import are separate steps.")
if fixture_mode():
    st.info("Simulation: these actions use a local fake Mealie service.")
output, source_root = paths()
service = ImportService(output, source_root)
try:
    views, history = catalog(), service.history()
except Exception as error:
    failure("Reading local records", error)
    st.stop()

ready = {view.candidate_id: view for view in views if view.import_ready and view.revision > 0}
views_by_id = {view.candidate_id: view for view in views}
st.subheader("Ready to import")
if not ready:
    st.info("Approve a complete recipe in Review to make it available here.")
if st.button("Select all eligible recipes", disabled=not ready):
    st.session_state.import_selection = list(ready)
if "import_selection" in st.session_state:
    st.session_state.import_selection = [value for value in st.session_state.import_selection if value in ready]
selected = st.multiselect("Recipes to import", list(ready), key="import_selection",
                         format_func=lambda value: ready[value].candidate.name)
selection_signature = tuple(sorted(selected))
prior_signature = st.session_state.get("import_tag_selection_signature")
if prior_signature != selection_signature:
    selected_tags = {str(getattr(ready[value], "batch_tag", "") or "").strip()
                     for value in selected}
    selected_tags.discard("")
    st.session_state.import_batch_tag = (
        next(iter(selected_tags)) if len(selected_tags) == 1 else ""
    )
    st.session_state.import_tag_selection_signature = selection_signature
tag = st.text_input(
    "Mealie tag (optional)", key="import_batch_tag",
    help="Defaults to the selected source batch's tag when all selected recipes share one. Existing history is never retagged.",
)
selected_tags = {str(getattr(ready[value], "batch_tag", "") or "").strip()
                 for value in selected}
selected_tags.discard("")
if len(selected_tags) > 1:
    st.caption("These recipes come from batches with different tag intent. Choose one tag for this import preflight; history will not change.")
signature = (tuple(selected), tag.strip())
if st.button("Preview import", disabled=not selected):
    try:
        st.session_state.import_plan = service.preview(selected, tag.strip())
        st.session_state.import_plan_signature = signature
    except Exception as error:
        failure("Import preview", error)
plan = st.session_state.get("import_plan")
if plan and st.session_state.get("import_plan_signature") == signature:
    with st.container(border=True):
        eligible = plan.get("eligible", [])
        st.markdown(f"**{len(eligible)} recipes will be sent to Mealie**")
        st.caption(f"Tag: {plan.get('batch_tag') or 'No additional tag'}. Only these exact approved revisions are included.")
        for item in eligible:
            method = ("manually approved without the original source"
                      if item.get("approval_method") == "manual_source_unavailable"
                      else "source-verified approval")
            st.write(f"{item['title']} · revision {item['revision']} · {method}")
            if item.get("approval_rationale"):
                st.caption(f"Review note: {item['approval_rationale']}")
            if item.get("no_instructions_reason"):
                st.caption(f"No-instructions decision: {item['no_instructions_reason']}")
        for item in plan.get("excluded", []):
            st.warning(item.get("reason", "This recipe is no longer eligible."))
        if st.button(f"Import {len(eligible)} {'recipe' if len(eligible) == 1 else 'recipes'}", type="primary", disabled=not eligible):
            try:
                st.session_state.import_results = connected_service().dispatch(plan["plan_id"])
                st.session_state.pop("import_plan", None)
                st.rerun()
            except Exception as error:
                failure("Import", error)
for item in st.session_state.get("import_results", []):
    creation = item.get("creation_status", "")
    message = ("Check whether Mealie received this recipe" if creation == "uncertain" else
               "Mealie rejected this import; review the recipe before retrying" if creation == "rejected" else
               "Imported · tag needs attention" if item.get("tag_status") in {"failed", "unknown"} else
               "Imported" if creation in {"created", "confirmed", "already_created", "linked_existing"} else
               item.get("reason") or "This recipe was not sent")
    st.info(f"{item.get('title', 'Recipe')} · {message}")

st.subheader("History")
search = st.text_input("Find in history", placeholder="Recipe title or source filename")
matches = [item for item in history if search.casefold() in (
    f"{item.get('title', '')} "
    f"{getattr(views_by_id.get(str(item.get('candidate_id') or item.get('identity') or '')), 'source_display_name', '') or human_source_name(item.get('source', ''))}"
).casefold()]
if not matches:
    st.info("No matching history yet." if search else "Imports and recovery actions will appear here.")
else:
    page = st.number_input("History page", min_value=1, max_value=max(1, math.ceil(len(matches) / 25)), step=1)
    st.caption(f"{len(matches)} history entries")
    for item in matches[(int(page) - 1) * 25:int(page) * 25]:
        identity = str(item.get("candidate_id") or item.get("identity") or item.get("key", ""))
        history_view = views_by_id.get(identity)
        with st.container(border=True):
            st.markdown(f"**{item.get('title') or 'Recipe'}**")
            status = str(item.get("creation_status") or item.get("status", item.get("state", ""))).upper()
            tag_failed = item.get("tag_status") == "failed"
            uncertain = status in {"UNCERTAIN", "REQUEST_STARTED", "PREPARED", "CHECK_MEALIE"}
            st.write("Check whether Mealie received this recipe" if uncertain else "Imported · tag needs attention" if tag_failed else "Imported" if status in {"IMPORTED", "CONFIRMED", "CREATED", "LINKED_EXISTING"} else "Import needs attention")
            source_label = (history_view.source_display_name if history_view
                            else human_source_name(item.get("source", "")))
            collection = f" · {history_view.batch_name}" if history_view else ""
            st.caption(f"{source_label}{collection} · {item.get('timestamp', '')}")
            if item.get("approval_method") == "manual_source_unavailable":
                st.caption("Approved manually without verification against the original source.")
                if item.get("approval_rationale"):
                    st.caption(f"Review note: {item['approval_rationale']}")
            if item.get("no_instructions_reason"):
                st.caption(f"No preparation instructions required: {item['no_instructions_reason']}")
            if item.get("slug"):
                st.write(f"Mealie recipe: {item['slug']}")
            repair_result_key = f"tag_repair_result:{identity}"
            if tag_failed and st.button("Retry tag only", key=f"repair:{identity}"):
                try:
                    st.session_state[repair_result_key] = connected_service().repair_tag(identity)
                    st.rerun()
                except Exception as error:
                    st.session_state[repair_result_key] = {
                        "tag_status": "failed", "tag_reason": f"{type(error).__name__}: check configuration and local records.",
                    }
                    failure("Tag repair", error)
            repair_result = st.session_state.get(repair_result_key)
            if repair_result:
                if repair_result.get("tag_status") in {"tagged", "already_tagged"}:
                    st.success("Tag repair succeeded. No recipe was created.")
                elif repair_result.get("tag_status") == "failed":
                    st.error(f"Tag repair failed: {repair_result.get('tag_reason') or 'Check the Mealie connection and retry.'}")
                else:
                    st.info(repair_result.get("reason") or "No tag repair was completed.")
                if st.button("Dismiss recovery result", key=f"dismiss_tag_result:{identity}"):
                    st.session_state.pop(repair_result_key, None)
                    st.rerun()
            reconciliation_feedback = item.get("reconciliation_feedback")
            if reconciliation_feedback:
                st.markdown("**Latest recovery action**")
                st.write(
                    f"{reconciliation_feedback.get('action') or 'Reconciliation'} Â· "
                    f"{reconciliation_feedback.get('result_status', '').replace('_', ' ').title()}"
                )
                if reconciliation_feedback.get("reason"):
                    st.caption(reconciliation_feedback["reason"])
                st.caption("Import protection remains active." if reconciliation_feedback.get("protection_active")
                           else "Import protection is not active.")
                if reconciliation_feedback.get("next_step"):
                    st.write("Next step: " + reconciliation_feedback["next_step"])
                feedback_action_id = str(reconciliation_feedback.get("action_id", ""))
                if st.button("Dismiss recovery result", key=f"dismiss_reconcile_feedback:{feedback_action_id}"):
                    service.dismiss_reconciliation_feedback(identity, feedback_action_id)
                    st.rerun()
            if uncertain:
                if item.get("attempt_status") == "orphaned":
                    st.warning("The create intent was saved, but no result was recorded. Mealie may still have received the request; do not retry this recipe.")
                st.caption("Creation is blocked until reconciliation. A missing search result is not proof of absence.")
                if item.get("attempt_id"):
                    st.caption(
                        f"Attempt {item['attempt_id']} · source {item.get('source_hash', '')} · "
                        f"extraction {item.get('extraction_id', '')} · revision {item.get('revision', '')} · "
                        f"content {item.get('content_sha256', '')} · intent {item.get('timestamp', '')}"
                    )
                if item.get("provider_response_evidence"):
                    st.caption(f"Provider response evidence: {item['provider_response_evidence']}")
                slug = st.text_input("Existing Mealie recipe slug", key=f"slug:{identity}")
                inspection_key = f"reconcile_inspection:{identity}"
                if st.button("Inspect existing recipe", key=f"inspect_reconcile:{identity}",
                             disabled=not slug.strip()):
                    action_id = uuid.uuid4().hex
                    active_service = service
                    try:
                        service.begin_reconciliation_action(
                            candidate_id=identity, action_id=action_id,
                            action="Inspect existing Mealie recipe",
                            attempt_id=str(item.get("attempt_id", "")), slug=slug.strip(),
                        )
                        active_service = connected_service()
                        result = active_service.reconcile_existing(
                            identity, slug.strip(), str(item.get("attempt_id", "")))
                        st.session_state[inspection_key] = _persist_reconciliation_result(
                            active_service, result, candidate_id=identity, action_id=action_id,
                            action="Inspect existing Mealie recipe",
                            attempt_id=str(item.get("attempt_id", "")), slug=slug.strip(),
                        )
                    except Exception as error:
                        failure_result = {
                            "creation_status": "not_verified", "reason":
                                "The selected recipe could not be verified. Local history and import protection remain in place.",
                            "attempt_id": str(item.get("attempt_id", "")), "slug": slug.strip(),
                        }
                        try:
                            st.session_state[inspection_key] = _persist_reconciliation_result(
                                active_service, failure_result, candidate_id=identity,
                                action_id=action_id, action="Inspect existing Mealie recipe",
                                attempt_id=str(item.get("attempt_id", "")), slug=slug.strip(),
                            )
                        except Exception:
                            st.session_state[inspection_key] = dict(
                                failure_result, candidate_id=identity, action_id=action_id,
                            )
                        failure("Reconciliation", error)
                    st.rerun()
                inspection = st.session_state.get(inspection_key)
                if (inspection and inspection.get("attempt_id") == item.get("attempt_id")
                        and inspection.get("slug", slug.strip()) == slug.strip()):
                    if inspection.get("creation_status") == "confirmation_required":
                        comparison = inspection["comparison"]
                        st.markdown("**Compare the approved recipe with the returned Mealie recipe**")
                        left, right = st.columns(2)
                        with left:
                            st.markdown("**Approved revision**")
                            st.write(comparison["local"]["title"] or "Untitled recipe")
                            st.markdown("Ingredients")
                            st.write(comparison["local"]["ingredients"] or ["No ingredients recorded"])
                            st.markdown("Instructions")
                            st.write(comparison["local"]["instructions"] or ["No instructions recorded"])
                            st.markdown("Yield")
                            st.write(comparison["local"]["yield_text"] or "No yield recorded")
                            st.markdown("Description")
                            st.write(comparison["local"]["description"] or "No description recorded")
                            st.markdown("Notes")
                            st.write(comparison["local"]["notes"] or ["No notes recorded"])
                        with right:
                            st.markdown("**Mealie recipe**")
                            st.write(comparison["mealie"]["title"] or "Untitled recipe")
                            st.markdown("Ingredients")
                            st.write(comparison["mealie"]["ingredients"] or ["No ingredients returned"])
                            st.markdown("Instructions")
                            st.write(comparison["mealie"]["instructions"] or ["No instructions returned"])
                            st.markdown("Yield")
                            st.write(comparison["mealie"]["yield_text"] or "No yield returned")
                            st.markdown("Description")
                            st.write(comparison["mealie"]["description"] or "No description returned")
                            st.markdown("Notes")
                            st.write(comparison["mealie"]["notes"] or ["No separate notes returned"])
                        if comparison["differences"]:
                            st.warning("Differences found: " + ", ".join(comparison["differences"]))
                        if comparison["unavailable_fields"]:
                            st.warning("Mealie did not return these fields, so they could not be compared: "
                                       + ", ".join(comparison["unavailable_fields"]))
                        if comparison["is_match"]:
                            st.info("All returned recipe fields match after formatting-only normalization.")
                        evidence = st.text_area(
                            "Evidence for linking this Mealie recipe to the uncertain import",
                            key=f"reconcile_evidence:{identity}:{item.get('attempt_id', '')}",
                            help="Record what you compared. A matching title alone is not identity evidence.")
                        material_ack = False
                        if comparison["material_differences"]:
                            material_ack = st.checkbox(
                                "I reviewed the recipe content differences and still confirm this is the created recipe.",
                                key=f"reconcile_material_ack:{identity}:{item.get('attempt_id', '')}")
                        unavailable_ack = False
                        if comparison["unavailable_fields"]:
                            unavailable_ack = st.checkbox(
                                "I understand the listed fields were not returned by Mealie and cannot be compared.",
                                key=f"reconcile_unavailable_ack:{identity}:{item.get('attempt_id', '')}")
                        if st.button("Confirm this existing recipe", key=f"confirm_reconcile:{identity}",
                                     disabled=not evidence.strip() or (
                                         bool(comparison["material_differences"]) and not material_ack) or (
                                         bool(comparison["unavailable_fields"]) and not unavailable_ack)):
                            action_id = uuid.uuid4().hex
                            active_service = service
                            try:
                                service.begin_reconciliation_action(
                                    candidate_id=identity, action_id=action_id,
                                    action="Confirm existing Mealie recipe",
                                    attempt_id=str(item.get("attempt_id", "")), slug=slug.strip(),
                                )
                                active_service = connected_service()
                                result = active_service.reconcile_existing(
                                    identity, slug.strip(), str(item.get("attempt_id", "")),
                                    reviewer_confirmed=True, evidence=evidence,
                                    material_difference_ack=material_ack,
                                    unavailable_field_ack=unavailable_ack,
                                    observed_recipe_digest=inspection["comparison_digest"])
                                st.session_state[inspection_key] = _persist_reconciliation_result(
                                    active_service, result, candidate_id=identity,
                                    action_id=action_id, action="Confirm existing Mealie recipe",
                                    attempt_id=str(item.get("attempt_id", "")), slug=slug.strip(),
                                )
                                st.rerun()
                            except Exception as error:
                                failure_result = {
                                    "creation_status": "not_verified", "attempt_id": item.get("attempt_id"),
                                    "slug": slug.strip(),
                                    "reason": f"{type(error).__name__}: check Mealie and local import history before retrying.",
                                }
                                try:
                                    st.session_state[inspection_key] = _persist_reconciliation_result(
                                        active_service, failure_result, candidate_id=identity,
                                        action_id=action_id, action="Confirm existing Mealie recipe",
                                        attempt_id=str(item.get("attempt_id", "")), slug=slug.strip(),
                                    )
                                except Exception:
                                    st.session_state[inspection_key] = dict(
                                        failure_result, candidate_id=identity, action_id=action_id,
                                    )
                                failure("Reconciliation", error)
                                st.rerun()
                    elif inspection.get("creation_status") in {"linked_existing", "already_linked"}:
                        st.success(f"Existing recipe linked for this attempt: {inspection.get('slug', '')}")
                    else:
                        st.error(inspection.get("reason") or "Reconciliation could not verify this recipe.")
                    if st.button("Dismiss recovery result", key=f"dismiss_reconcile_result:{identity}"):
                        st.session_state.pop(inspection_key, None)
                        st.rerun()
            else:
                inspection_key = f"reconcile_inspection:{identity}"
                inspection = st.session_state.get(inspection_key)
                if inspection and inspection.get("creation_status") in {"linked_existing", "already_linked"}:
                    st.success(f"Existing recipe linked for this attempt: {inspection.get('slug', '')}")
                    if st.button("Dismiss recovery result", key=f"dismiss_reconcile_result:{identity}"):
                        st.session_state.pop(inspection_key, None)
                        st.rerun()
