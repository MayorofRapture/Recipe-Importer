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
