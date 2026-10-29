# Mealie compatibility and large cookbook PDF capability

## F8: empty instructions

### Current evidence

- The repository does not identify the exact installed Mealie release or provide a versioned OpenAPI response for the user's server.
- `.env.example` keeps `MEALIE_SUPPORTS_EMPTY_INSTRUCTIONS=false` by default.
- Import preflight blocks an empty-instructions recipe unless that explicit local setting is enabled.
- The offline client contract test verifies that the current Mealie v3 adapter sends `recipeInstructions: []` unchanged to `/api/recipes/create/html-or-json` and does not synthesize a step.
- Mock import tests cover the current wrapper accepting the request, a server rejection, and preventing an uncertain/rejected attempt from being posted repeatedly.

These tests establish the outgoing request and importer safeguards. They do **not** establish how any installed Mealie version stores the recipe, whether it adds a default step, or what a subsequent GET returns. Live compatibility is **Blocked** pending version-matched verification and explicit authorization for one disposable Mealie write. The global gate remains disabled.

### Safe acceptance procedure when authorized

1. Record the exact Mealie version and confirm the test target is a disposable instance or a separately backed-up staging database running the same release and configuration as the intended server.
2. Confirm no recipe with the test slug or unique test title already exists. Use one disposable seasoning recipe with a unique title, one ingredient, and an intentionally empty instruction list.
3. Keep `MEALIE_SUPPORTS_EMPTY_INSTRUCTIONS=false` during setup. Authorize exactly one create request against the disposable target; do not run the whole import batch.
4. Inspect the captured request body and confirm `recipeInstructions` is an empty list. Inspect the create response, then GET that exact returned slug and verify the persisted instruction list is still empty and no localized/default step was inserted.
5. Record the Mealie version, request/response shape, persisted result, and cleanup result without retaining credentials or private recipe content. Delete only the uniquely identified disposable recipe after explicit cleanup authorization.
6. Enable the setting only for that verified installation, then restart the app and run the single-recipe preflight. If the response is ambiguous, the recipe is protected for reconciliation; do not repeat the create.

Without the exact server version and authorization in step 3, leave the gate disabled and the item blocked.

## F9: whole-book PDFs and page ranges

### Recipe-sized source behavior

- Ordinary `BatchStore.stage()` uploads retain the 50 MiB file cap and 100-page PDF cap. The existing Upload page continues using this path.
- Existing ordinary sources remain content-addressed entries in `sources`; their hashes, IDs, extraction behavior, and job behavior are unchanged.
- Ordinary uploaded PDFs can still be sent as whole-file extraction inputs. No selected-page-range field exists in batch/job/source identity.

### Phase 3B.1: backend-only parent PDF staging

`BatchStore.stage_parent_pdf(batch_id, source_path)` adds a separate intake API for a trusted local path. It does not relax ordinary upload limits and is not called by the UI, OCR, extraction, or Mealie import code.

- Defaults are 256 MiB per parent PDF, 2,000 pages, and a 64 MiB free-space reserve. These are explicit guardrails, not a promise that every downstream PDF operation can process a file at those maxima.
- The copy streams in 1 MiB chunks, incrementally hashes, checks for source changes, and rechecks remaining free space every 16 MiB. It validates the copied file by path with PyMuPDF, flushes and fsyncs it, then publishes it atomically under the batch's `parent_documents` directory.
- New metadata uses a versioned `parent_pdf` record with the full SHA-256 as its stable `document_id`. Existing manifests without `parent_documents` remain readable. Parent records do not enter `sources`, and `JobService` continues building jobs from `sources` only.
- Corrupt, encrypted, empty, unsupported, changed, and over-cap PDFs are rejected. Interrupted temporary copies are cleaned up. A manifest-write failure leaves a content-addressed file that an identical retry can register. Duplicate content creates no second record; if its owned staged file is missing or corrupt, retry restores it only to the validated in-library content path.
- A synthetic 60 MiB staging probe passed with a sampled process working-set increase of 2.16 MiB at 5 ms sampling. This is one local observation, not a formal maximum-memory guarantee.

This milestone only stages the immutable parent. It does **not** add page ranges, page-range jobs, automatic splitting, a picker, OCR, extraction, review, or import of parent documents. Whole-book PDF processing remains **Unsupported** until the later range milestones are implemented and separately verified.

### Local path-picker feasibility and security

`run_app.bat` starts a local Streamlit server. Streamlit still runs a server and a per-session script thread, while a browser may be on a different machine. The [Streamlit threading guide](https://docs.streamlit.io/develop/concepts/design/multithreading) documents this separation and warns that attaching Streamlit context to custom threads is unsupported. The [architecture guide](https://docs.streamlit.io/develop/concepts/architecture/architecture) distinguishes browser and server machines. A server-side native dialog therefore cannot select a file from a remote browser's computer.

The regular [`st.file_uploader`](https://docs.streamlit.io/develop/api-reference/widgets/st.file_uploader) flow transfers uploaded content to the server and exposes it as an uploaded-file object; it does not provide the client's local path. Its normal in-memory upload object is not a suitable large-book path. A future remote flow would need a bounded chunk-to-disk upload mechanism rather than passing a browser-supplied filesystem path to the server.

On the same Windows host, a native dialog could select a host-local path, but the current Streamlit script thread is not a conventional GUI event-loop thread. Python's [Tkinter threading notes](https://docs.python.org/3.14/library/tkinter.html#threading-model) explain that Tk calls depend on its event loop and thread model. No headed Windows proof-of-concept has established that a blocking Tk dialog integrates reliably with Streamlit reruns, and starting it on an arbitrary worker thread is not approved as a design.

The picker decision remains **Unverified**. Before adding UI, choose and demonstrate either a reliable same-host desktop bridge or a bounded browser-mediated transfer. Never accept an arbitrary server path from a browser request. A future implementation must obtain a selected path only through a trusted local picker or allowlisted root, validate regular-file/PDF status, sanitize its display name, copy into app-owned storage, and never use the selected file path as a destination. Remote upload must enforce the same file/page/disk caps and keep path handling server-side.

### Deferred implementation design

Page-range support needs a durable document-to-range identity model, so it is deferred rather than implemented as a limit increase.

1. Keep the original PDF immutable and identify it by its full SHA-256, page count, and accepted storage path.
2. Add an explicit selected range `(original_hash, first_page, last_page)` as a child source. Give every range a deterministic ID derived from the original hash and inclusive page bounds; store parent book ID and page provenance in batch/job events.
3. Create a bounded temporary PDF containing only the selected pages with PyMuPDF page-copy operations. Validate page bounds and output size before extraction; always clean up task-owned temporary files after completion or recovery.
4. Include original hash, range bounds, model/prompt/schema configuration, and extraction version in the range cache identity. Preserve prior range caches and attempt records on retries.
5. Bind each candidate to the parent document hash and page range as well as its extraction identity. Do not use title or array position to match candidates across ranges or re-extractions.
6. Treat overlapping ranges as separate extraction evidence until a reviewer compares their source locations and content. Provide explicit duplicate/reconciliation decisions; never suppress a recipe solely because the title matches.
7. Resume by range and recipe outcome. Completed ranges and successful recipes remain protected; interrupted or failed ranges can be retried without replacing the original book or prior outputs.
8. Add a review UI for page thumbnails, range entry/selection, and page-bound provenance. Keep range selection explicit before extraction and show the source pages beside every candidate.

Before release, add synthetic PDFs with more than 100 pages, duplicate titles on different pages, overlapping ranges, multi-recipe ranges, interrupted range retries, changed source bytes, and bounded-memory rendering. Verify old whole-file batch records remain readable and no original PDF is rewritten.
