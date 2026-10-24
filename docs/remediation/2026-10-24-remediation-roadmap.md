# Recipe-Importer Evidence-Based Remediation Roadmap

**Date:** 2026-10-24
**Repository:** `MayorofRapture/Recipe-Importer`
**Branch:** `feature/complete-recipe-workspace`
**Audit baseline:** `c31c47b6124f46ee5855efc34a48717c3470652e`
**Audit:** `docs/reviews/2026-10-24-repository-workflow-audit.md`

## Scope and authority

This roadmap records Phases 1 and 2 plus the completed Phase 3B.1 backend staging milestone. Phase 3A remains the design basis; page-range work in Phase 3B.2 onward requires separate approval after independent review. Phase 4 is deferred maintenance. A comprehensive visual/interaction redesign is a separate project and is outside this roadmap's implementation scope.

No phase authorizes a live Gemini or Mealie call, a production data change, a rewrite of production JSONL records, a PR merge, or changes to `.vscode/` or generated Graphify output. Keep `MEALIE_SUPPORTS_EMPTY_INSTRUCTIONS=false` until the separately authorized, version-matched Mealie check described in the compatibility document is completed.

## Phase 1 — Draft-save feedback (AUD-003)

**Status:** Completed and verified.
**Objective:** Show reliable feedback after a saved revision, and accurately distinguish a no-op save.

### Root cause

`app_pages/review.py::_save()` called `st.success()` and then called `st.rerun()` when it created a new revision. The transient alert disappeared during the rerun. `_render_actions()` already exposed session feedback through `review_save_state`, but the draft-save action did not populate that state. The editor is a Streamlit fragment, so a content edit reruns that fragment without rerunning the outer action area.

### Correction

- Reused `review_save_state`; no notification framework or durable notification storage was added.
- Successful state is bound to candidate ID, saved revision, and the buffer's saved content signature. The message is rendered inside the editor fragment after widget values are synchronized.
- The message is cleared if the selected candidate, buffer revision, or content signature no longer matches. A subsequent edit therefore cannot appear saved, and changing candidates cannot show another candidate's confirmation.
- A newly persisted revision says `Draft saved (revision N).`; a no-op save says `Draft already saved (revision N).` The existing `save_draft()` no-op/revision authority remains the persistence boundary.
- Failed saves continue to show the error and retain the in-memory editor values. Approval, import, and recipe-history behavior were not changed.

### Changed files and evidence

| File | Change |
|---|---|
| `app_pages/review.py` | Bind, render, and invalidate ephemeral draft-save feedback; report no-op saves distinctly. |
| `tests/test_streamlit_review.py` | Cover a changed save and durable history, clearing feedback after edits, candidate isolation, no-op idempotence, and retained edits/error on failure. |
| `tools/browser_upload_journey.mjs` | Exercise edit → save → edit → save → no-op → reload, then continue through approval and simulated import. |

**Implementation commit:** `913bbd86c3a0e20638ed433af7ca638fe4e2b322` (`fix: preserve draft save confirmation`).
**Targeted tests:** 25 tests reported `OK` in the captured focused-run log; the outer command returned an anomalous nonzero status, so the full-suite process exit is the authoritative offline result.
**Full offline suite:** 488 tests passed (`unittest`, 328.593 seconds, PowerShell process exit 0).
**Browser journey:** Synthetic Chromium journey passed through edit, save, edit, second save, no-op save, reload, approval, and fake import; process exit 0. No live service behavior was exercised.
**Compilation / CLI help / whitespace:** Targeted `compileall` and credential-free CLI `--help` exited 0; Node harness `--check` passed; `git diff --check` was clean; synthetic browser fixture tests passed 9/9 after rerunning outside a sandbox process-spawn restriction.
**Windows CI for the exact implementation SHA:** Passed. Workflow `Offline verification`, Windows job, run [40120715088](https://github.com/MayorofRapture/Recipe-Importer/actions/runs/40120715088), completed successfully for `913bbd86c3a0e20638ed433af7ca638fe4e2b322`.

The Chromium journey runs against the repository's marked synthetic library and fake services. Its pass does not verify Gemini or live Mealie behavior.

## Phase 2 — Accessible source-text review (AUD-002)

**Status:** Completed and verified under the user-approved Phase 2 implementation request.
**Goal:** Let a keyboard and screen-reader user inspect available source-derived text for the exact page beside the existing scan and editable recipe. The source-text region is read-only and does not change recipe data, approval, or import projections.

### Existing building blocks

- `local_ocr.py::_ocr_pdf()` extracts selectable PDF text page by page and OCRs pages without selectable text. It emits page markers (`--- PDF PAGE N ---` and `--- OCR PAGE N ---`) and retains original/layout variants.
- `import_recipes.py::_ocr_text_path()`, `_ocr_text_paths()`, and `_save_ocr_variants()` persist selected, original, and layout text under the source content hash and versioned names in `output/ocr_text/`.
- OCR-only rows and OCR-assisted extraction results already carry cache paths, engine/method, selected variant, and selection rationale in their reports/job results. OCR-structure cache metadata records its source hash and selected variant, but not every legacy artifact has complete method metadata.
- `ui_data.py::render_source_page()` renders one verified local page; `app_pages/review.py::_render_source()` keeps that image and page selector visible.
- `verify_selected_source()` and the catalog bind candidate review to a source hash and current source availability. The text view must preserve that boundary.
- OCR and source-preview tests already cover page markers, original/layout/selected variants, source caching, and missing source behavior. Extend these contracts rather than adding a second OCR pipeline.

### Implemented lookup contract

Added `ui_data.source_text_for_page(view, page_number, output_dir) -> SourceTextPage` beside the existing source-preview helpers. It validates the selected source identity/hash and returns the exact one-based page text (or empty), method/provenance, uncertainty notes, page mapping, and a user-readable status. It reads selectable PDF text or existing hash-bound OCR variants and never writes or regenerates OCR caches.

Lookup order:

1. Confirm the selected view and source hash are current. Do not look up text using title, array position, or the currently selected candidate alone.
2. For a PDF, read selectable text directly from the exact PyMuPDF page when present. This is local extraction, not a Gemini call. Label it “Selectable PDF text” and note that graphics or scanned text may be absent.
3. Otherwise, locate existing OCR variants using the existing source-hash/version path contract. Select the recorded selected variant when metadata is available; label original/layout fallback truthfully when selected text is absent. Never overwrite or regenerate a variant during review.
4. Split cached text by its explicit PDF/OCR page markers and return only the selected page. If a multi-page legacy cache has no usable page markers, report that page mapping is unavailable. Do not show whole-document OCR under every page.
5. If cache text or metadata is missing, malformed, or unreadable, return a user-readable state and keep the scan visible. Do not fall back to a different source's cache. For an unavailable source with a hash-bound cache, expose it only with a prominent “Original source unavailable; this text cannot be checked against the scan” notice; keep existing approval/source-integrity gates unchanged.
6. Existing Gemini-derived recipe fields are not transcription and must never be used as a source-text fallback. No cloud request is permitted by this design.

If a cached OCR page is unavailable, the UI shows the reason and leaves the existing review path intact. A separate, explicit local-OCR action for sources with no readable cache is not included until its resource bounds, consent wording, background-job ownership, and recovery behavior are separately specified and approved. It must not silently invoke Gemini.

### Implemented Review presentation

Keep the current visual scan and page control. Add a separately headed, read-only “Source text” region in the source pane, tied to that same page selection. Use native Streamlit text/heading elements, not editable recipe widgets or custom HTML. Show the original page number, method, cache/variant provenance where known, uncertainty, and empty/unavailable explanations. Preserve OCR text as a reading aid; the recipe editor remains a separate, authoritative draft. No OCR text is automatically copied into a recipe field.

The text region is exposed in document order using native Streamlit elements, and its heading distinguishes source text from recipe ingredients and instructions. Text is not stored in session state; page text and metadata reads have file-size limits and bounded process caches.

### Verification evidence

- [x] Hash-bound image OCR and selectable/scanned PDF tests return only the requested page; variant provenance and OCR uncertainty are reported.
- [x] Markerless multi-page caches, duplicate markers, oversized/unreadable caches, missing sources, source-hash mismatch, and candidate cache isolation are covered by unit tests.
- [x] AppTest verifies page-bound source text, the unavailable-source warning, and unchanged review/import history.
- [x] Synthetic Chromium verified the source-text heading/content and page control in the accessibility tree, keyboard navigation to page 2, page-specific text, visible keyboard focus, and existing edit/approval/fake-import workflows.
- [x] Source-text contrast measured 13.33:1 in the rendered browser; body text was 16px and provenance was 14px.
- [x] Full offline suite: 501 tests passed (`unittest discover -s tests -v`, 375.444 seconds, process exit 0). Targeted compilation, credential-free CLI `--help`, browser harness syntax, and `git diff --check` passed.
- [ ] Screen-reader announcements were not verified. Windows Narrator is installed, but the synthetic Chromium run is headless and no interactive Narrator session was exercised.
- [ ] The audit is limited to the changed source-text panel and page control. App-wide 200% resize, 320px reflow, all focus-obscuring cases, and target sizes were not verified; this is not a full WCAG conformance claim.

**Implementation commit:** `ec37f04fe6ff9c7c4a8e1eff199b8a1310eafc01` (`feat: show page-bound source text in review`).
**Windows CI for the exact implementation SHA:** Passed. Workflow `Offline verification`, Windows job, run [40123240419](https://github.com/MayorofRapture/Recipe-Importer/actions/runs/40123240419), completed successfully for `ec37f04fe6ff9c7c4a8e1eff199b8a1310eafc01`.
**Live services:** No Gemini or Mealie calls were made.

Where legacy method provenance was not persisted, the UI labels the variant as unrecorded or reports a selected-cache fallback rather than inferring a method.

## Phase 3A — Resumable whole-book PDF design (AUD-001)

**Authorization:** This phase is the design basis. Phase 3B.1 was separately authorized and implemented as backend-only staging. Phase 3B.2 and later require separate approval after independent review of their range, job, and UI contracts.
**Goal:** Process a cookbook as an immutable parent PDF with durable, user-selected, resumable page-range children. Do not implement by merely raising the current limits.

### Current constraints

`BatchStore.stage()` continues accepting recipe-sized byte sources with a 50 MiB file limit and 100-page PDF limit. Phase 3B.1 adds a separate path-based parent-PDF staging API with larger explicit bounds, but does not add page ranges or job dispatch. `ui_job_service.JobService` records attempts and outcomes per ordinary source ID and resumes waiting/failed sources. `import_recipes.process_file()` and the current caches use whole-file hashes. `local_ocr._ocr_pdf()` already produces page-marked text, and `ui_data.render_source_page()` renders one page of an ordinary source. Candidate identities currently use source hash plus a content fingerprint; matching must not use title or the `recipes[]` position.

### Proposed parent and range records

Keep original page numbers one-based and range endpoints inclusive.

```text
BookDocument v1
  document_id = full original SHA-256
  batch_id, original_filename, immutable_staged_path
  size_bytes, total_page_count, created_at

PageRange v1
  range_id = SHA-256("book-range:v1" + document_id + start_page + end_page)
  document_id, batch_id, start_page, end_page
  status, attempt_count, attempts[], result_ids[], error
```

The existing `digest`/source hash remains the hash of its source bytes and retains its old meaning. Do not alter existing candidate IDs, approvals, import records, or historical whole-file jobs. Range identity and provenance are additive, explicitly versioned fields. New candidate identity must incorporate the range ID as well as the existing source/content basis; repeated identical titles or content from different pages must remain distinct until a person compares them. Store original SHA, range ID/bounds, generated range-input hash, and extraction ID with each registration/job/cache so current review approval can still validate the immutable original and exact range evidence.

### Parent intake above 50 MiB

The existing Upload path calls `UploadedFile.getvalue()` and `BatchStore.stage()` accepts an in-memory `bytes`; raising either configured bound would keep the whole parent in memory and would remove an important safety limit. Phase 3B.1 therefore adds `BatchStore.stage_parent_pdf(batch_id, source_path)` as a separate backend path. Its default cap is 256 MiB and 2,000 pages, with a 64 MiB free-space reserve. It streams 1 MiB chunks, hashes incrementally, checks available space every 16 MiB, validates the staged path with PyMuPDF, fsyncs, and atomically publishes versioned parent metadata. It preserves ordinary `sources` and uses the existing manifest lock and atomic-write behavior.

**Implemented in Phase 3B.1:** the backend accepts a trusted local PDF path and copies it in bounded chunks into isolated, content-addressed parent storage. It rejects invalid, encrypted, changed, or over-limit files; preserves the original; cleans partial copies; safely recovers a content file after a manifest-write failure; and restores missing duplicate content only to a validated in-library path. Versioned records are additive under `parent_documents`. `JobService` continues to select only `sources`, so parent PDFs cannot enter OCR, extraction, or Mealie import.

**UI selection remains unresolved.** `run_app.bat` is local, but no headed Windows proof-of-concept verifies a native picker on Streamlit's script thread. A remote browser cannot supply a server-local path; remote use would need a bounded chunk upload that writes to server-owned temporary storage and enforces the same size/disk/page caps. Do not pass browser-supplied path strings to `stage_parent_pdf`, and do not use regular `st.file_uploader` for oversized books. The 256 MiB cap is an explicit staging guardrail; it is not a downstream processing-capacity claim. See the picker evidence and source links in `docs/mealie-compatibility-and-large-pdf-capability.md`.

### Range identity, extraction, and cache strategy

- Parent document identity is full original SHA-256, with immutable path, filename, length, total pages, and batch link. Verify digest before creating ranges or approving the resulting evidence.
- User-created ranges use stable IDs from parent hash plus inclusive bounds. Re-adding the same range to the same book is idempotent. Overlapping ranges are allowed but explicitly shown.
- Use bounded PyMuPDF page-copy into an ephemeral range input; do not alter the original. Validate page bounds and copied output size/page count. Hash the generated range bytes incrementally.
- Cache key binds original hash, range ID/bounds, generated range hash, extractor/OCR version, model and prompt/schema version where used. Preserve earlier range caches and attempts. A changed original invalidates its child ranges; it creates a new parent ID and never overwrites old results.
- Each job range has independent waiting/processing/completed/failed/quota-paused status, attempt timestamps, outcome, candidate IDs, and error. Resume only unfinished ranges. Within a completed range, preserve successful recipe outcomes if another candidate needs attention.
- Extraction may return several recipes for a range. Bind every candidate to its range location; no array-index matching. Same title on different pages and overlapping-range duplicate observations stay separate until explicit human reconciliation. Do not auto-deduplicate by title or suppress a recipe because a successful import exists elsewhere.
- Keep `source_hash` semantics and parent bytes verifiable for current approval authority. Review must display original page numbers and range bounds while rendering the original PDF. Candidate IDs include range identity so separate occurrences do not collide. Approval remains bound to exact candidate, source hash, extraction, and revision.
- Keep old whole-file source/job records readable. New readers treat missing range metadata as a legacy whole-file source. No production `import_state.jsonl` or review event history is rewritten.

### Job and UI workflow

1. Create/select the cookbook batch and tag as today.
2. Choose a local PDF; show filename, size, page count, and any existing parent with the same hash before staging. Do not silently attach a duplicate to a different batch.
3. Navigate the original by page number and preview start/end pages. Add explicit inclusive ranges. Keep source selection visible; show overlaps and allow edit/remove before extraction.
4. MVP uses manual page ranges. It does not auto-split at fixed page counts because that can split a recipe across ranges. Optional consecutive range suggestions may prefill controls, but every boundary must be previewed and confirmed; they are not automatically processed. The operator keeps multi-page recipes together by selecting all their pages in one range. Multiple recipes on a page/range are supported.
5. Start and pause/resume the durable job. Progress reports completed/waiting/failed ranges and discovered recipes, with recipe-level outcomes within each range.
6. Review candidates beside the original PDF at the recorded page/range. Navigation to the source uses the original PDF, not an unlinked temporary range file.
7. Import still uses the current selected-approved-recipe flow, exact review projection, duplicate protection, ambiguous-outcome reconciliation, and separate tag outcome.

### Persistence, compatibility, recovery, and rollback

Add optional versioned parent/range metadata to new batches and job records, or use a dedicated append-only range event ledger if code inspection shows that changing the batch manifest would make legacy recovery unsafe. Do not migrate old entries in place. Use atomic writes/locks consistent with `BatchStore` and `JobService`; preserve the existing file source list and job reader. Persist a range attempt before work begins and terminal outcome after work completes. On crash, detect stale processing under the existing job lock and return only that range to recoverable waiting state. Never remove parent PDFs, prior range input/cache, or successful recipe/import records as part of retry.

Rollback is feature-disablement, not data deletion: stop offering new parent/range intake and dispatch; retain originals, range events, attempts, and caches; keep readers for v1 metadata; let legacy file jobs continue. Do not restore an older binary that cannot read new metadata until compatibility is verified. Re-enabling requires a successful read/resume check on preserved synthetic records.

### Phase 3B.1 implementation evidence and remaining milestones

Use synthetic files only; no production recipe data or service writes.

| Milestone | Boundary | Required evidence |
|---|---|---|
| **3B.1 Parent staging — completed** | Backend-only bounded path intake and additive `BatchStore` parent metadata | >50 MiB and >100-page PDFs within explicit cap; over-cap, low-disk, corrupt/encrypted, interrupted temporary copy, changed bytes; original unchanged; duplicate and recovery behavior; no parent-to-job dispatch. |
| 3B.2 Range creation | Deterministic inclusive IDs and bounded PyMuPDF copy | Same range idempotent; bounds validated; overlap recorded; generated bytes hash and page count stable; parent hash/path preserved. |
| 3B.3 Range jobs/cache | JobService per-range attempts/resume and cache keys | Multi-range partial failure, completed ranges not repeated, interrupted resume, changed model/config/source invalidation, legacy jobs readable. |
| 3B.4 Candidate and review provenance | Range-aware registration, source preview/page mapping, reconciliation | Multi-recipe pages, recipes spanning pages, duplicate titles, overlapping ranges; distinct candidates; exact source hash/revision approval; ambiguous duplicates held. |
| 3B.5 Upload/review interaction | Range picker and progress/recovery UI | Streamlit AppTest plus synthetic Chromium for add/edit ranges, overlap, resume, page mapping, and recovery. Review checklist and accessibility evidence. |
| 3B.6 Compatibility gate | Existing workflows and safety | Run `tests/test_batch_store.py`, `tests/test_ui_jobs.py`, `tests/test_ui_data.py`, `tests/test_review_identity_authorization.py`, `tests/test_review_reconciliation.py`, `tests/test_streamlit_upload.py`, relevant full suite, CLI help, compilation, browser journeys, and `git diff --check`; inspect legacy fixtures and confirm no live services. |

**3B.1 implementation commit:** `7e15c3ceeb9b086659c562c7bc17e2ee83014ee1` (`feat: stage parent cookbook PDFs safely`), pushed from baseline `e82d293400220e8cd7f75e81cea89a9c578eb2fd`.

**3B.1 verification:** 517 offline tests passed with 1 capability skip (`unittest discover -s tests`, 326.267 seconds, process exit 0). The batch/job subset passed 44/44. Targeted Python compilation, CLI `--help`, and `git diff --check` passed. Synthetic Chromium review/import and historical recovery journeys passed; the Windows `Offline verification` workflow passed for the exact implementation SHA in both the [push run](https://github.com/MayorofRapture/Recipe-Importer/actions/runs/40126488768) and [PR run](https://github.com/MayorofRapture/Recipe-Importer/actions/runs/40126492197). A synthetic 60 MiB staging probe measured a 2.16 MiB sampled working-set increase at 5 ms intervals; this is one observation, not a guaranteed upper bound.

No UI picker, range creation, OCR, extraction, range job, review, or Mealie import for parent PDFs was added. Same-host native picker integration remains **Unverified** pending a headed Windows proof-of-concept. For remote browsers, a native dialog would select on the server host; any later remote selection needs an authenticated, bounded chunk-to-disk transfer and must never accept a browser-supplied server path. Details and official Streamlit/Tkinter references are in `docs/mealie-compatibility-and-large-pdf-capability.md`.

Do not begin 3B.2 or later until the range identity, job/cache, candidate provenance, UI, and rollback contracts receive independent review and separate user approval.

## Phase 4 — CLI/UI primitive ownership (AUD-004)

**Status:** Deferred maintenance; no refactor in this assignment.

`ui_data.py` and `ui_import_service.py` import `StateStore`, `file_digest`, and candidate identity helpers from `import_recipes.py`; `ui_job_service.py` imports extraction functions from it lazily. The CLI module therefore also acts as a shared library. The audit found coupling, not a current operational failure.

Before any future extraction, document all call sites and persisted ID/ledger contracts for `candidate_identity()`, `_identity_for_title()`, `StateStore`, and `file_digest()`. Demonstrate a concrete maintenance benefit beyond moving code between files. If justified, move only these stable primitives to one narrowly scoped module and re-export them from `import_recipes.py` for compatibility. Keep IDs, import-state JSONL, review events, error handling, and CLI flags unchanged. Add exact historical identity fixtures and run CLI, catalog, job, import-service, and full offline tests. Do not split `ReviewStore` or the Review page as part of this task.

## Separate future UI/UX redesign

The audit and Phase 1 browser journey do not establish that the entire interface is visually effective or efficient. A comprehensive Review redesign remains a separate project. Preserve current function and authority boundaries until a new design scope is approved. A future design should begin with the existing `docs/UI_UX_Audit.md`, `docs/UI_Product_Experience.md`, `docs/UI_Redesign_Roadmap.md`, and project `recipe-review-ux` checklist; it should address repetitive review, source-to-recipe comparison, clear next action, missing detail, and interruption/re-entry. This roadmap does not redesign the UI.

## Safety checklist

- [x] Phase 1 changes are limited to draft feedback and its necessary regression journey/tests.
- [x] Phase 2 source-text review is implemented and verified; Phase 3A remains a specification only.
- [x] Phase 3B.1 implements backend-only parent staging; range handling, picker UI, OCR/extraction, parent job dispatch, and parent import remain unimplemented.
- [x] Phase 4 remains deferred maintenance.
- [x] No live Gemini or Mealie calls were made.
- [x] Production import/review histories, source scans, extraction caches, and recipe records were not modified.
- [x] `.vscode/` and `graphify-out/` were preserved and excluded from commits.
- [x] PR #3 was not merged.

## Phase gates

Phases 1 and 2 and Phase 3B.1 are complete with passing offline tests, synthetic Chromium journeys where applicable, compilation/help/diff checks, pushed implementation commits, and Windows CI verified at each implementation SHA. Phase 3B.2 onward awaits independent review of the remaining design and separate implementation approval. Phase 4 remains deferred unless a concrete maintenance benefit and compatibility plan are approved. No phase automatically authorizes the next.
