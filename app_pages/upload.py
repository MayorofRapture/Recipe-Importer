"""Upload local recipe scans and start resumable extraction jobs."""
from __future__ import annotations

import hashlib
from pathlib import Path

import streamlit as st

from app_pages.common import paths
from batch_store import BatchStore
from runtime_safety import fixture_mode
from ui_data import render_source_page
from ui_job_service import GEMINI, LOCAL_OCR, OCR_ASSISTED, JobService



def _job_service(output_dir: Path) -> JobService:
    return _cached_job_service(str(output_dir.resolve()), fixture_mode())


@st.cache_resource(show_spinner=False)
def _cached_job_service(output_path: str, demo_mode: bool) -> JobService:
    output = Path(output_path)
    if demo_mode:
        from fixture_extraction import create_fixture_extractor
        return JobService(output, extractor=create_fixture_extractor(output), background=True)
    return JobService(output)


@st.fragment(run_every="2s")
def _render_job_panel(output_path: Path, batch_id: str) -> None:
    service = _job_service(output_path)
    job = next((item for item in service.list_jobs() if item.get("batch_id") == batch_id), None)
    if job is None:
        st.info("No extraction job has been started for this batch.")
        return
    st.markdown("#### Extraction progress")
    st.info(_job_summary(job), icon=":material/autorenew:")
    progress = job.get("progress", {})
    total = int(progress.get("total", 0))
    completed = int(progress.get("completed", 0))
    st.progress(completed / total if total else 0.0, text=f"{completed} of {total} files finished")
    unfinished = sum(1 for source in job.get("sources", [])
                     if source.get("status") in {"waiting", "processing", "failed", "quota_paused"})
    st.caption(
        f"{job.get('candidate_count', 0)} recipes ready for review · "
        f"{job.get('failure_count', 0)} files need attention"
    )
    if job.get("status") in {"failed", "completed_with_errors"}:
        st.caption(f"{unfinished} files unfinished. Completed extraction results are preserved.")
        st.info("Completed file results and extraction history are preserved. Retry processes only unfinished files.")
    if job.get("status") == "failed" and job.get("error"):
        st.warning(f"Extraction stopped: {job['error']}")
    if job.get("quota_paused"):
        st.warning("Gemini has reached an available request allowance. Completed files are saved; resume later to continue unresolved files.")
    if job.get("status") in {"queued", "waiting_for_worker", "running", "pausing"}:
        if st.button("Pause after current file", key=f"pause_{job['job_id']}"):
            service.pause(job["job_id"])
            st.rerun()
    elif job.get("status") in {"paused", "quota_paused", "completed_with_errors", "failed"}:
        if st.button("Resume unfinished files", key=f"resume_{job['job_id']}"):
            service.resume(job["job_id"])
            st.rerun()
    if job.get("status") in {"completed", "completed_with_errors"}:
        st.success("Extraction is complete. Review each candidate before approving or importing it.")
        st.caption("Open the Review page from the navigation to check the extracted candidates.")
    failures = [source for source in job.get("sources", []) if source.get("status") == "failed"]
    for source in failures:
        st.warning(f"{source.get('original_filename', 'A file')} needs attention: {source.get('error') or 'Review this file and try again.'}")


def _stage_selected_files(store: BatchStore, batch: dict, uploads) -> list[dict]:
    if not uploads:
        return []
    key = f"upload_stage_results:{batch['batch_id']}"
    seen_key = f"upload_staged_digests:{batch['batch_id']}"
    results = st.session_state.setdefault(key, [])
    seen = set(st.session_state.get(seen_key, []))
    for uploaded in uploads:
        data = uploaded.getvalue()
        digest = hashlib.sha256(data).hexdigest()
        if digest in seen:
            existing = next((source for source in batch.get("sources", [])
                             if source.get("digest") == digest), None)
            if existing:
                path = Path(str(existing.get("staged_path", "")))
                try:
                    if path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == digest:
                        continue
                except OSError:
                    pass
            # The UI-level dedupe cache must not prevent an exact re-upload from
            # restoring a staged source that was removed or damaged on disk.
            seen.discard(digest)
        result = store.stage(batch["batch_id"], uploaded.name, data)
        results.append({"filename": uploaded.name, "digest": digest, **result})
        seen.add(digest)
    st.session_state[seen_key] = sorted(seen)
    return results


def _render_stage_result(result: dict, store: BatchStore) -> None:
    status = result.get("status")
    if status == "staged":
        st.success(f"Saved {result.get('filename') or result.get('original_filename')}.")
    elif status in {"duplicate", "relinked"}:
        existing_batch = store.get(result.get("existing_batch_id", "")) or {}
        if status == "relinked":
            st.success(f"Restored {result.get('filename')} for its existing recipe work.")
            st.button("Open existing work", key=f"open_relinked_{result.get('existing_source_id')}",
                      on_click=_select_existing_batch, args=(result.get("existing_batch_id"),))
        elif result.get("existing_batch_id") == result.get("batch_id"):
            st.info(f"{result.get('filename')} is already in this batch.")
        else:
            label = existing_batch.get("name") or "the earlier batch"
            st.warning(f"{result.get('filename')} is already in “{label}”. No duplicate was added.")
            st.button("Open existing work", key=f"open_duplicate_{result.get('existing_source_id')}",
                      on_click=_select_existing_batch, args=(result.get("existing_batch_id"),))
    else:
        st.error(f"Could not add {result.get('filename')}: {result.get('message', 'The file was rejected.')}")
        message = str(result.get("message", "")).casefold()
        if "could not save" in message or "metadata could not be committed" in message:
            st.button("Retry saving this file", key=f"retry_stage_{result.get('digest')}",
                      on_click=_retry_stage, args=(result.get('digest', ''),))


def _select_existing_batch(batch_id: str) -> None:
    st.session_state["upload_batch_id"] = batch_id


def _retry_stage(digest: str) -> None:
    key = f"upload_staged_digests:{st.session_state.get('upload_batch_id', '')}"
    st.session_state[key] = [item for item in st.session_state.get(key, []) if item != digest]


def _batch_label(batch: dict) -> str:
    sources = len(batch.get("sources", []))
    noun = "file" if sources == 1 else "files"
    return f"{batch['name']} · {sources} {noun}"


def _source_status(status: str) -> str:
    return {"staged": "Ready", "processing": "Being read", "completed": "Read",
            "no_recipe": "No recipe detected", "failed": "Needs attention",
            "quota_paused": "Waiting for Gemini"}.get(status, "Waiting")


def _job_summary(job: dict) -> str:
    status = job.get("status")
    if status in {"queued", "waiting_for_worker"}:
        return "Your files are waiting to be read."
    if status in {"running", "pausing"}:
        return "Your files are being read. You can keep this page open or return later."
    if status == "paused":
        return "Extraction is paused. Completed files are saved and will not be repeated."
    if status == "quota_paused":
        return "Extraction paused after reaching a Gemini request allowance. Completed files are saved."
    if status == "completed_with_errors":
        return "Extraction finished; some files need attention. Resume to retry only unfinished files."
    if status == "failed":
        return "Extraction stopped before all files finished. Check the failure details, then retry unfinished files."
    return "All files finished extraction. Review the candidates before approval."


def _ranges_overlap(first_page: int, last_page: int, other_first: int, other_last: int) -> bool:
    return first_page <= other_last and other_first <= last_page


def _set_range_edit(batch_id: str, document_id: str, index: int, pending: list[dict]) -> None:
    selected = pending[index]
    st.session_state[f"cookbook_first:{batch_id}:{document_id}"] = selected["first_page"]
    st.session_state[f"cookbook_last:{batch_id}:{document_id}"] = selected["last_page"]
    st.session_state[f"cookbook_edit:{batch_id}:{document_id}"] = index


def _remove_pending_range(batch_id: str, document_id: str, index: int) -> None:
    key = f"cookbook_pending:{batch_id}:{document_id}"
    pending = st.session_state.get(key, [])
    if 0 <= index < len(pending):
        pending.pop(index)
    st.session_state[f"cookbook_edit:{batch_id}:{document_id}"] = None


def _render_cookbook_workflow(store: BatchStore, batch: dict, output_dir: Path) -> None:
    """Connect staged parent PDFs to the existing page-range and range-job APIs."""
    batch_id = str(batch["batch_id"])
    st.markdown("#### Cookbook PDFs")
    st.markdown(
        "For a whole cookbook, launch `run_cookbook_selector.bat` from Windows, "
        "choose this batch and a PDF, then refresh this list. The selector runs "
        "as a separate desktop app; this page never opens a file dialog or accepts a local path."
    )
    st.button("Refresh staged cookbooks", key=f"refresh_cookbooks:{batch_id}")

    batch = store.get(batch_id) or batch
    parents = batch.get("parent_documents", [])
    if not parents:
        st.info("No cookbook PDFs are staged for this batch yet. Ordinary recipe uploads above are unchanged.")
        return

    parent_ids = [str(item.get("document_id", "")) for item in parents]
    parent_by_id = {str(item.get("document_id", "")): item for item in parents}
    selected_document_id = st.selectbox(
        "Staged cookbook PDF", parent_ids,
        format_func=lambda value: parent_by_id[value].get("original_filename", "Cookbook PDF"),
        key=f"cookbook_parent:{batch_id}",
    )
    parent = parent_by_id[selected_document_id]
    page_count = int(parent.get("page_count", 0))
    size_bytes = int(parent.get("size_bytes", 0))
    st.caption(
        f"Original file: {parent.get('original_filename', 'Cookbook PDF')} · "
        f"{size_bytes:,} bytes · {page_count:,} pages · {parent.get('status', 'staged')}"
    )
    if page_count < 1:
        st.error("This staged cookbook has no usable pages. Refresh the list or inspect its batch record.")
        return

    notice_key = f"cookbook_range_notice:{batch_id}:{selected_document_id}"
    notice = st.session_state.get(notice_key)
    if notice:
        if notice[0] == "error":
            st.error(notice[1])
        else:
            st.success(notice[1])

    preview_page = st.number_input(
        "Preview original page", min_value=1, max_value=page_count, value=1, step=1,
        key=f"cookbook_preview:{batch_id}:{selected_document_id}",
    )
    preview = render_source_page(Path(parent["staged_path"]), int(preview_page), zoom=1)
    if preview.image is not None:
        st.image(preview.image, alt=f"Original cookbook page {preview.page_number}")
    else:
        st.warning(preview.message or "The selected cookbook page could not be previewed.")

    st.caption(
        f"Choose inclusive page bounds. Each range is limited to {store.max_page_range_pages} pages "
        f"and {store.max_page_range_bytes // (1024 * 1024)} MiB. Keep a multi-page recipe in one range; "
        "one range may contain multiple recipes. The app will not split recipes automatically."
    )
    first_key = f"cookbook_first:{batch_id}:{selected_document_id}"
    last_key = f"cookbook_last:{batch_id}:{selected_document_id}"
    st.session_state.setdefault(first_key, 1)
    st.session_state.setdefault(last_key, 1)
    first_page = int(st.number_input("First page", min_value=1, max_value=page_count,
                                    step=1, key=first_key))
    last_page = int(st.number_input("Last page", min_value=1, max_value=page_count,
                                   step=1, key=last_key))
    proposed_count = last_page - first_page + 1
    valid_bounds = first_page <= last_page and proposed_count <= store.max_page_range_pages
    if valid_bounds:
        st.caption(f"Proposed range: pages {first_page}-{last_page} · {proposed_count} pages")
    elif first_page > last_page:
        st.error("The last page must be the same as or after the first page.")
    else:
        st.error(f"A page range can contain at most {store.max_page_range_pages} pages.")

    pending_key = f"cookbook_pending:{batch_id}:{selected_document_id}"
    pending = st.session_state.setdefault(pending_key, [])
    edit_key = f"cookbook_edit:{batch_id}:{selected_document_id}"
    edit_index = st.session_state.get(edit_key)
    action_label = "Save range edit" if edit_index is not None else "Add range to selection"
    if st.button(action_label, key=f"cookbook_add:{batch_id}:{selected_document_id}",
                 disabled=not valid_bounds):
        proposed = {"first_page": first_page, "last_page": last_page}
        duplicate_pending = any(index != edit_index and item == proposed
                                for index, item in enumerate(pending))
        if duplicate_pending:
            st.error("That exact page range is already in the uncommitted selection.")
        elif edit_index is not None and 0 <= edit_index < len(pending):
            pending[edit_index] = proposed
            st.session_state[edit_key] = None
            st.rerun()
        else:
            pending.append(proposed)
            st.rerun()

    if pending:
        st.markdown("**Uncommitted ranges**")
        for index, item in enumerate(pending):
            columns = st.columns([3, 1, 1], vertical_alignment="center")
            columns[0].write(f"Pages {item['first_page']}-{item['last_page']} · "
                             f"{item['last_page'] - item['first_page'] + 1} pages")
            columns[1].button(
                "Edit", key=f"cookbook_edit_button:{batch_id}:{selected_document_id}:{index}",
                on_click=_set_range_edit, args=(batch_id, selected_document_id, index, pending),
                width="stretch",
            )
            columns[2].button(
                "Remove selected range", key=f"cookbook_remove:{batch_id}:{selected_document_id}:{index}",
                on_click=_remove_pending_range, args=(batch_id, selected_document_id, index),
                width="stretch",
            )

    ranges = sorted(parent.get("ranges", []), key=lambda item: (item.get("first_page", 0), item.get("last_page", 0)))
    if ranges:
        st.markdown("**Previously committed ranges**")
        for item in ranges:
            st.caption(
                f"Pages {item.get('first_page')}-{item.get('last_page')} · "
                f"{item.get('page_count')} pages · {item.get('status', 'saved')} · "
                f"ID `{item.get('range_id', '')}`"
            )

    overlap_pairs = []
    committed = [(int(item.get("first_page", 0)), int(item.get("last_page", 0))) for item in ranges]
    for index, item in enumerate(pending):
        bounds = (int(item["first_page"]), int(item["last_page"]))
        if any(_ranges_overlap(*bounds, *prior) for prior in committed):
            overlap_pairs.append(index)
        if any(_ranges_overlap(*bounds, int(prior["first_page"]), int(prior["last_page"]))
               for prior in pending[:index]):
            overlap_pairs.append(index)
    requires_overlap_confirmation = bool(overlap_pairs)
    overlap_scope = tuple(
        (index, int(pending[index]["first_page"]), int(pending[index]["last_page"]))
        for index in sorted(set(overlap_pairs))
    )
    overlap_scope_key = f"cookbook_overlap_scope:{batch_id}:{selected_document_id}"
    overlap_checkbox_key = f"cookbook_overlap_confirm:{batch_id}:{selected_document_id}"
    if st.session_state.get(overlap_scope_key) != overlap_scope:
        st.session_state[overlap_checkbox_key] = False
        st.session_state[overlap_scope_key] = overlap_scope
    if requires_overlap_confirmation:
        st.warning("These page ranges overlap. Each selected range will be extracted as a separate occurrence; "
                   "review similar recipes yourself before deciding what to keep.")
    overlap_confirmed = st.checkbox(
        "I understand these page ranges overlap", key=overlap_checkbox_key,
        disabled=not requires_overlap_confirmation,
    )
    if pending and st.button(
        "Commit selected page ranges", type="primary",
        key=f"cookbook_commit:{batch_id}:{selected_document_id}",
        disabled=requires_overlap_confirmation and not overlap_confirmed,
    ):
        remaining = []
        failures = []
        saved_count = 0
        for item in pending:
            result = store.create_page_range(
                batch_id, selected_document_id, item["first_page"], item["last_page"],
            )
            if result.get("status") == "rejected":
                remaining.append(item)
                failures.append(f"Pages {item['first_page']}-{item['last_page']}: "
                                f"{result.get('message', 'The range was rejected.')}")
            else:
                saved_count += 1
        st.session_state[pending_key] = remaining
        st.session_state[edit_key] = None
        if failures:
            prefix = (f"{saved_count} range(s) were saved; " if saved_count else "")
            st.session_state[notice_key] = (
                "error", prefix + "some page ranges were not saved: " + " ".join(failures)
                + " The rejected selections remain available to edit or retry."
            )
        else:
            st.session_state[notice_key] = (
                "success", "Selected page ranges were saved. The original cookbook remains unchanged."
            )
        st.rerun()

    if ranges:
        range_ids = [str(item["range_id"]) for item in ranges]
        range_by_id = {str(item["range_id"]): item for item in ranges}
        selected_ranges = st.multiselect(
            "Ranges to extract", range_ids,
            format_func=lambda value: f"Pages {range_by_id[value]['first_page']}-"
                                      f"{range_by_id[value]['last_page']} · "
                                      f"{range_by_id[value]['page_count']} pages",
            key=f"cookbook_extract_ranges:{batch_id}:{selected_document_id}",
        )
        mode_labels = ["Local OCR", "Gemini extraction", "OCR-assisted review"]
        mode_by_label = {"Local OCR": LOCAL_OCR, "Gemini extraction": GEMINI,
                         "OCR-assisted review": OCR_ASSISTED}
        mode_label = st.segmented_control(
            "Cookbook extraction method", mode_labels, default="Local OCR",
            key=f"cookbook_mode:{batch_id}:{selected_document_id}",
        )
        mode = mode_by_label.get(mode_label or "Local OCR", LOCAL_OCR)
        service = _job_service(output_dir)
        config_fingerprint = service.range_config_fingerprint(mode)
        cloud_consent = False
        consent_widget_key = "cookbook_cloud_consent"
        consent_scope_key = "cookbook_cloud_consent_scope"
        consent_scope = (batch_id, selected_document_id, tuple(sorted(selected_ranges)), mode, config_fingerprint)
        reset_consent_key = "cookbook_cloud_consent_reset"
        reset_consent_for = st.session_state.pop(reset_consent_key, None)
        if st.session_state.get(consent_scope_key) != consent_scope or reset_consent_for == consent_scope:
            st.session_state[consent_widget_key] = False
        st.session_state[consent_scope_key] = consent_scope
        if mode in {GEMINI, OCR_ASSISTED}:
            if fixture_mode():
                st.info("Demonstration only: cloud modes use synthetic output and send no cookbook pages to Google.")
                consent_label = "I understand this cloud mode is simulated and sends no pages to Google."
            else:
                st.warning("Cloud cookbook extraction sends selected pages, or their OCR text, to Google.")
                consent_label = "I agree to send the selected cookbook pages to Google for extraction."
            cloud_consent = st.checkbox(consent_label, key=consent_widget_key)

        jobs = [item for item in service.list_range_jobs()
                if item.get("batch_id") == batch_id
                and any(value.get("parent_document_id") == selected_document_id
                        for value in item.get("ranges", []))]
        active_statuses = {"queued", "waiting_for_worker", "running", "pausing", "paused", "quota_paused"}
        active_job = next((item for item in jobs if item.get("status") in active_statuses), None)
        prior_selection = next((item for item in jobs
                                if item.get("mode") == mode
                                and set(item.get("range_ids", [])) == set(selected_ranges)
                                and item.get("config_fingerprint") == config_fingerprint), None)
        if prior_selection and prior_selection.get("status") in {"completed", "completed_with_errors"}:
            st.info("These selected ranges already have saved extraction results. Review them or resume unfinished ranges; they will not be started again.")
        disabled = not selected_ranges or active_job is not None or prior_selection is not None or (
            mode in {GEMINI, OCR_ASSISTED} and not cloud_consent
        )
        if st.button("Start selected range extraction", type="primary",
                     key=f"cookbook_start:{batch_id}:{selected_document_id}", disabled=disabled):
            try:
                job = service.start_range_job(batch_id, selected_ranges, mode, cloud_consent=cloud_consent)
                st.session_state[f"cookbook_last_job:{batch_id}"] = job.get("job_id", "")
                if mode in {GEMINI, OCR_ASSISTED}:
                    st.session_state[reset_consent_key] = consent_scope
                st.rerun()
            except (OSError, RuntimeError, ValueError) as exc:
                st.error(f"Cookbook extraction could not start: {exc}")

        for job in jobs[:8]:
            _render_range_job(job, service, str(parent.get("original_filename", "Cookbook PDF")))


@st.fragment(run_every="2s")
def _render_range_job(job: dict, service: JobService, parent_filename: str) -> None:
    job_id = str(job.get("job_id", ""))
    current = service.range_snapshot(job_id) or job
    st.markdown("#### Cookbook extraction progress")
    status_copy = {
        "queued": "Waiting to start", "waiting_for_worker": "Waiting for an available worker",
        "running": "Reading selected pages", "pausing": "Finishing the current range before pausing",
        "paused": "Paused; finished ranges are saved", "quota_paused": "Paused at a Gemini request limit",
        "completed": "Extraction finished", "completed_with_errors": "Finished with ranges needing attention",
        "failed": "Extraction stopped; unfinished ranges can be resumed",
    }
    st.info(status_copy.get(current.get("status"), "Cookbook extraction status is available below."))
    ranges = current.get("ranges", [])
    st.caption(f"Original cookbook: {parent_filename} · selected inclusive page ranges")
    completed = sum(item.get("status") in {"completed", "no_recipe"} for item in ranges)
    st.progress(completed / len(ranges) if ranges else 0.0,
                text=f"{completed} of {len(ranges)} ranges finished")
    st.caption(f"{sum(int(item.get('result_count', 0)) for item in ranges)} recipes extracted · "
               f"Job {job_id}")
    for item in ranges:
        label = {"waiting": "Waiting", "processing": "Being read", "completed": "Complete",
                 "no_recipe": "No recipe detected", "failed": "Needs attention",
                 "quota_paused": "Waiting for Gemini"}.get(item.get("status"), item.get("status", "Waiting"))
        st.write(f"Pages {item.get('first_page')}-{item.get('last_page')} · {label} · "
                 f"{item.get('result_count', 0)} recipes")
        if item.get("error"):
            st.warning(item["error"])

    if current.get("status") in {"completed", "completed_with_errors"}:
        try:
            registered = service.register_completed_ranges(job_id)
            count = len(registered)
            if count:
                st.success(f"{count} cookbook recipes are ready for review.")
            elif completed:
                st.success("Completed cookbook results are registered for Review.")
        except (OSError, RuntimeError, ValueError) as exc:
            st.error(f"Completed results could not be registered for Review: {exc}")
        st.caption("Cookbook range recipes can be reviewed and approved, but Mealie import remains disabled.")
        st.caption("Open Review from the app navigation to check these candidates against their original pages.")

    if current.get("status") in {"queued", "waiting_for_worker", "running", "pausing"}:
        if st.button("Pause after current range", key=f"pause_range:{job_id}"):
            service.pause_range_job(job_id)
            st.rerun(scope="app")
    elif current.get("status") in {"paused", "quota_paused", "completed_with_errors", "failed"}:
        if st.button("Resume unfinished ranges", key=f"resume_range:{job_id}"):
            service.resume_range_job(job_id)
            st.rerun(scope="app")


st.title("Upload recipes")
st.caption("Add recipe scans, choose how to read them, then review the results before any import.")

output_dir, _source_root = paths()
store = BatchStore(output_dir)
batches = store.list_batches()

with st.form("create_upload_batch", border=False):
    st.subheader("Create a batch")
    new_name = st.text_input("Batch name", placeholder="For example, Sunday recipes", key="new_batch_name")
    new_tag = st.text_input("Cookbook tag (optional)", placeholder="Applied only to recipes imported from this batch", key="new_batch_tag")
    create_batch = st.form_submit_button("Create batch", icon=":material/create_new_folder:")

if create_batch:
    try:
        created = store.create(new_name, new_tag)
        st.session_state["upload_batch_id"] = created["batch_id"]
        st.session_state["upload_notice"] = f"Created {created['name']}. Add scans below."
        st.rerun()
    except (OSError, ValueError, RuntimeError) as exc:
        st.error(f"The batch could not be created: {exc}")

batches = store.list_batches()
batch_ids = [item["batch_id"] for item in batches]
selected_id = st.session_state.get("upload_batch_id")
if selected_id not in batch_ids:
    selected_id = batch_ids[0] if batch_ids else ""
    st.session_state["upload_batch_id"] = selected_id

if batches:
    selected_id = st.selectbox(
        "Select an existing batch", batch_ids,
        format_func=lambda value: _batch_label(next(item for item in batches if item["batch_id"] == value)),
        key="upload_batch_id",
    )

notice = st.session_state.pop("upload_notice", "")
if notice:
    st.success(notice)

if not selected_id:
    st.info("Create a batch to begin. Each accepted file is saved as soon as it passes validation.")
    st.stop()

batch = store.get(selected_id)
if batch is None:
    st.error("This batch is no longer available. Choose another batch or create a new one.")
    st.stop()

st.subheader(batch["name"])
if batch.get("tag"):
    st.caption(f"Cookbook tag for future imports: {batch['tag']}")

uploads = st.file_uploader(
    "Add PDF, JPG, PNG, or WEBP scans",
    type=["pdf", "jpg", "jpeg", "png", "webp"],
    accept_multiple_files=True,
    key=f"upload_files_{selected_id}",
    help="Each file is validated and saved independently, so one invalid file does not discard accepted files.",
)
upload_results = _stage_selected_files(store, batch, uploads)
for result in upload_results:
    _render_stage_result(result, store)

batch = store.get(selected_id) or batch
sources = batch.get("sources", [])
batch_jobs = [item for item in JobService(output_dir).list_jobs()
              if item.get("batch_id") == selected_id]
job_source_states = {
    source["source_id"]: source.get("status", "")
    for job in reversed(batch_jobs)
    for source in job.get("sources", [])
}
if sources:
    st.markdown("#### Files in this batch")
    for source in sources:
        pages = source.get("page_count", 1)
        page_label = f"{pages} page" if pages == 1 else f"{pages} pages"
        job_status = job_source_states.get(source["source_id"], source.get("status", ""))
        status_label = _source_status(job_status)
        st.markdown(f"**{source['original_filename']}** · {page_label} · {status_label}")
    preview_source = sources[-1]
    preview = render_source_page(Path(preview_source["staged_path"]), 1)
    if preview.image is not None:
        st.image(preview.image, caption=f"First page · {preview_source['original_filename']}",
                 alt=f"First page preview of {preview_source['original_filename']}")
    elif preview.message:
        st.caption(preview.message)
else:
    st.info("No files have been accepted yet. Add one or more local scans to continue.")

st.markdown("#### Extraction")
mode_labels = ["Local OCR", "Gemini extraction", "OCR-assisted review"]
mode_by_label = {"Local OCR": LOCAL_OCR, "Gemini extraction": GEMINI,
                 "OCR-assisted review": OCR_ASSISTED}
mode_label = st.segmented_control(
    "Choose an extraction method", mode_labels, default="Local OCR",
    key=f"upload_mode_{selected_id}",
)
mode = mode_by_label.get(mode_label or "Local OCR", LOCAL_OCR)
if fixture_mode():
    st.info("Demonstration only: each method uses synthetic placeholder output. No scans are sent to Gemini.")
elif mode == LOCAL_OCR:
    st.caption("Local OCR reads scans on this computer. It does not send recipe content to a cloud service.")
else:
    if mode == GEMINI:
        st.warning("Gemini extraction sends the selected recipe scans to Google for processing.")
    else:
        st.warning("OCR-assisted review sends the selected scan and OCR text to Google for recipe structuring.")
cloud_consent = False
if mode in {GEMINI, OCR_ASSISTED}:
    consent_label = ("I understand this extraction is simulated in the demonstration library."
                     if fixture_mode() else
                     "I agree to send these recipe scans to Google for the selected extraction.")
    cloud_consent = st.checkbox(consent_label, key=f"upload_cloud_consent_{selected_id}")

try:
    service = _job_service(output_dir)
except (OSError, RuntimeError, ValueError) as exc:
    service = None
    st.error(f"Extraction is unavailable: {exc}")

if service is not None:
    jobs = [item for item in service.list_jobs() if item.get("batch_id") == selected_id]
    handled_source_ids = {
        source.get("source_id")
        for job in jobs
        for source in job.get("sources", [])
        if source.get("source_id")
    }
    new_sources = [source for source in sources if source.get("source_id") not in handled_source_ids]
    active_job = next((item for item in jobs if item.get("status") in {
        "queued", "waiting_for_worker", "running", "pausing", "paused", "quota_paused",
    }), None)
    if active_job is not None:
        _render_job_panel(output_dir, selected_id)
        if new_sources:
            st.info("New files are saved and will be available after the current extraction finishes.")
    elif new_sources or not jobs:
        if jobs and new_sources:
            st.info("Previously finished files will be kept; extraction will process only the new files.")
        if st.button("Start extraction", type="primary", disabled=not sources,
                     key=f"start_extraction_{selected_id}"):
            try:
                service.start(selected_id, mode, cloud_consent=cloud_consent)
                st.rerun()
            except (OSError, RuntimeError, ValueError) as exc:
                st.error(f"Extraction could not start: {exc}")
    else:
        _render_job_panel(output_dir, selected_id)

_render_cookbook_workflow(store, batch, output_dir)
