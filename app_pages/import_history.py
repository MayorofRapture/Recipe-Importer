"""Selected exact-revision imports and independent durable history."""
import math
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
            if tag_failed and st.button("Retry tag only", key=f"repair:{identity}"):
                try:
                    connected_service().repair_tag(identity)
                    st.rerun()
                except Exception as error:
                    failure("Tag repair", error)
            if uncertain:
                st.caption("Creation is blocked until reconciliation. A missing search result is not proof of absence.")
                slug = st.text_input("Existing Mealie recipe slug", key=f"slug:{identity}")
                if st.button("Verify existing recipe", key=f"reconcile:{identity}", disabled=not slug.strip()):
                    try:
                        connected_service().reconcile_existing(identity, slug.strip())
                        st.rerun()
                    except Exception as error:
                        failure("Reconciliation", error)
