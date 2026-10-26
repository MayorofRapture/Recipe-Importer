# Recipe-Importer Evidence-Based Remediation Roadmap

**Date:** 2026-10-24
**Repository:** `MayorofRapture/Recipe-Importer`
**Branch:** `feature/complete-recipe-workspace`
**Audit baseline:** `c31c47b6124f46ee5855efc34a48717c3470652e`
**Audit:** `docs/reviews/2026-10-24-repository-workflow-audit.md`

## Scope and authority

This roadmap records Phases 1 and 2 plus the completed Phase 3B.1 parent staging, Phase 3B.2 page-range creation, Phase 3B.3 durable range jobs/cache, Phase 3B.4 candidate/review provenance, and Phase 3B.5 upload/review interaction milestones. Phase 3A remains the design basis; Phase 3B.6 remains gated for independent review and separate authorization. Phase 4 is deferred maintenance. A comprehensive visual/interaction redesign is a separate project and is outside this roadmap's implementation scope.

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

**Authorization:** This phase is the design basis. Phases 3B.1 and 3B.2 were separately authorized and implemented as backend-only parent staging and range creation. Phase 3B.3 and later require separate approval after independent review of their job, cache, provenance, and UI contracts.
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

PageRange v1 (3B.2)
  range_id = SHA-256(NUL-join("recipe-importer:page-range:v1", parent_sha256, first_page, last_page))
  document_id, batch_id, first_page, last_page, page_count
  input_path, input_sha256, created_at, status, record_version

RangeJob v1 (3B.3 target)
  status, attempt_count, attempts[], result_ids[], error
```

The existing `digest`/source hash remains the hash of its source bytes and retains its old meaning. Do not alter existing candidate IDs, approvals, import records, or historical whole-file jobs. Range identity and provenance are additive, explicitly versioned fields. Phase 3B.4 uses `range-v1:` candidate IDs bound to the original parent SHA, batch/range ID and inclusive bounds, generated range-input hash, extraction identity, canonical recipe-content fingerprint, and occurrence number among identical recipes in that range. Extraction identity is derived from the recipe-content multiset, so result-array ordering does not define identity. Identical recipe occurrences remain distinct; repeated titles or content in different ranges remain separate until a person compares them. Registrations retain the extraction cache's producing job, configuration identity, and digest. The registration `source_hash` remains the actual original parent PDF SHA-256, allowing existing review approval to validate the immutable source; range provenance is separate and explicit.

### Parent intake above 50 MiB

The existing Upload path calls `UploadedFile.getvalue()` and `BatchStore.stage()` accepts an in-memory `bytes`; raising either configured bound would keep the whole parent in memory and would remove an important safety limit. Phase 3B.1 therefore adds `BatchStore.stage_parent_pdf(batch_id, source_path)` as a separate backend path. Its default cap is 256 MiB and 2,000 pages, with a 64 MiB free-space reserve. It streams 1 MiB chunks, hashes incrementally, checks available space every 16 MiB, validates the staged path with PyMuPDF, fsyncs, and atomically publishes versioned parent metadata. It preserves ordinary `sources` and uses the existing manifest lock and atomic-write behavior.

**Implemented in Phase 3B.1:** the backend accepts a trusted local PDF path and copies it in bounded chunks into isolated, content-addressed parent storage. It rejects invalid, encrypted, changed, or over-limit files; preserves the original; cleans partial copies; safely recovers a content file after a manifest-write failure; and restores missing duplicate content only to a validated in-library path. Versioned records are additive under `parent_documents`. `JobService` continues to select only `sources`, so parent PDFs cannot enter OCR, extraction, or Mealie import.

**UI selection was resolved in Phase 3B.5.** A separate user-launched Windows process uses a native picker on its main thread and calls `BatchStore.stage_parent_pdf()` directly. The headed Windows proof used an isolated batch and synthetic three-page PDF; the real dialog opened, the PDF was selected, and batch metadata showed the staged filename and page count. The Streamlit page accepts no filesystem path and never opens a picker. A remote browser still cannot supply a server-local path; remote intake would need a bounded transfer into server-owned temporary storage with the existing size, disk, and page caps. The 256 MiB cap is a staging guardrail, not a downstream processing-capacity claim. See `docs/mealie-compatibility-and-large-pdf-capability.md` for the existing picker safety contract.

### Range identity, extraction, and cache strategy

- Parent document identity is full original SHA-256, with immutable path, filename, length, total pages, and batch link. Verify digest before creating ranges or approving the resulting evidence.
- User-created ranges use stable IDs from parent hash plus inclusive bounds. Re-adding the same range to the same book is idempotent. Overlapping ranges are allowed but explicitly shown.
- Use bounded PyMuPDF page-copy into a canonical persisted range input; do not alter the original. Validate page bounds and copied output size/page count. Hash the generated range bytes incrementally and retain them with a per-range recovery receipt so a manifest-write retry can recover the exact bytes.
- Cache key binds original hash, range ID/bounds, generated range hash, extractor/OCR version, model and prompt/schema version where used. Preserve earlier range caches and attempts. A changed original invalidates its child ranges; it creates a new parent ID and never overwrites old results.
- Each job range has independent waiting/processing/completed/failed/quota-paused status, attempt timestamps, outcome, candidate IDs, and error. Resume only unfinished ranges. Within a completed range, preserve successful recipe outcomes if another candidate needs attention.
- Extraction may return several recipes for a range. Bind every candidate to its range location; no array-index matching. Same title on different pages and overlapping-range duplicate observations stay separate until explicit human reconciliation. Do not auto-deduplicate by title or suppress a recipe because a successful import exists elsewhere.
- Keep `source_hash` semantics and parent bytes verifiable for current approval authority. Review must display original page numbers and range bounds while rendering the original PDF. Candidate IDs include range identity so separate occurrences do not collide. Approval remains bound to exact candidate, source hash, extraction, and revision.
- Keep old whole-file source/job records readable. New readers treat missing range metadata as a legacy whole-file source. No production `import_state.jsonl` or review event history is rewritten.

### 3B.4.1 - production range extraction (completed)

The authorized extraction-only adapter connects durable range jobs to local OCR and configured Gemini modes. It does not change the recipe review UI, create approval events, or import recipes. Production execution is selected explicitly and remains separate from the synthetic fixture extractor. It returns serialized recipe results without calling `ReviewStore`, writing ordinary extraction caches, or invoking `import_recipes.process_file()`.

- **Local OCR and OCR-assisted:** reuse the local OCR and structuring/reference helpers while retaining OCR provenance and diagnostics in the versioned range cache. Source-level OCR paths remain unchanged.
- **Gemini:** use `import_recipes.extract()` with `ModelRouter` and the shared `UsageLedger`. Provider requests are consent-checked and reserved against configured usage limits; quota exhaustion pauses the durable job. Existing bounded transient retries remain accounted for.
- **Consent:** cloud modes require explicit persisted consent and timestamp before start/resume, with a second check immediately before each provider request. Legacy records without consent remain unconsented. Local OCR needs no cloud consent.
- **Identity/cache:** production range results use a distinct versioned cache kind and credential-free fingerprint over range input and extraction configuration. Synthetic and legacy cache compatibility is preserved; mismatched execution kinds fail closed.
- **Provenance handoff:** `register_completed_ranges()` remains the sole candidate-registration boundary. Extraction itself does not write review or import ledgers. Range candidates remain excluded from import by the existing safeguards.
- **Recovery:** per-range cache reuse, quota pause/resume, input verification, and retry behavior remain durable. Uncertain or mismatched cache records fail closed.

**Verification:** synthetic/offline tests cover the three extraction modes, consent, usage reservations, quota handling, cache-kind isolation, provenance, and side-effect boundaries. No live Gemini or Mealie calls were made. See the implementation evidence below.

**Rollback:** disable production range dispatch while retaining readers for all existing job/cache schema versions. Preserve range PDFs, original parent PDFs, attempts, caches, and registrations; do not delete or migrate them.

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

### Phase 3B implementation evidence and remaining milestones

Use synthetic files only; no production recipe data or service writes.

| Milestone | Boundary | Required evidence |
|---|---|---|
| **3B.1 Parent staging — completed** | Backend-only bounded path intake and additive `BatchStore` parent metadata | >50 MiB and >100-page PDFs within explicit cap; over-cap, low-disk, corrupt/encrypted, interrupted temporary copy, changed bytes; original unchanged; duplicate and recovery behavior; no parent-to-job dispatch. |
| **3B.2 Range creation - completed** | Deterministic inclusive IDs, bounded PyMuPDF copy, and additive parent range metadata | Same range idempotent; bounds validated; overlap recorded; generated bytes hash and page count verified; parent hash/path preserved; failure and retry recovery; no range-to-job dispatch. |
| **3B.3 Range jobs/cache - completed** | Durable per-range attempts, recovery, and immutable synthetic-result cache; no live extraction | Multi-range partial failure, completed work not repeated, interrupted recovery, config/source invalidation, fail-closed execution, quota pause/resume, cache integrity, stable job identity, legacy ordinary jobs unchanged; 550-test offline suite and exact-SHA Windows CI. |
| **3B.4 Candidate and review provenance - completed** | Durable range-aware candidate registration, catalog projection, source page mapping, and range-import exclusion | 14 new registration/provenance regressions; identical occurrences and titles, overlapping ranges, config changes, parent/source integrity, partial recovery and idempotency; 571-test offline suite (1 capability skip); 54 focused range/job tests; synthetic review/import Chromium journey passed. The separate P0 browser runner timed out waiting for its expected recipe option and remains unresolved. Windows CI for implementation SHA `509377f5171c9a89ee143dea164a5843f192bf30` passed in [run 50](https://github.com/MayorofRapture/Recipe-Importer/actions/runs/40225261944). No live Gemini or Mealie call. |
| **3B.4.1 Production range extraction - completed** | Consent-gated extraction-only adapter for local OCR, Gemini, and OCR-assisted modes; registration stays at `register_completed_ranges()` | Six production-adapter regressions; 578-test offline suite (1 capability skip); 56 focused production/UI/model-routing tests; exact-SHA Windows CI passed. Synthetic browser review journey passed; upload/P0 browser harnesses could not start because sandbox denied spawning Python (`EPERM`). No live providers. |
| **3B.5 Upload/review interaction - completed** | Staged cookbook discovery, inclusive range selection, extraction progress/recovery controls, source mapping, and import exclusion | 8 focused cookbook AppTests; 607-test offline suite (2 capability skips); synthetic Chromium journey passed in Windows CI; headed Windows native-dialog and staging proof; 9 fixture-runner tests; exact-SHA Windows CI. Accessibility evidence is limited to rendered control names and 1440px viewport measurements in the CI journey plus labeled Streamlit widgets/AppTests; 320px reflow and full keyboard/screen-reader conformance remain unverified. |
| **3B.6 Compatibility gate - audited (Conditional Pass)** | Existing workflows, historical formats, recovery, import safety, and UI regression | See the dated Phase 3B.6 audit below. |

**3B.1 implementation commit:** `7e15c3ceeb9b086659c562c7bc17e2ee83014ee1` (`feat: stage parent cookbook PDFs safely`), pushed from baseline `e82d293400220e8cd7f75e81cea89a9c578eb2fd`.

**3B.1 verification:** 517 offline tests passed with 1 capability skip (`unittest discover -s tests`, 326.267 seconds, process exit 0). The batch/job subset passed 44/44. Targeted Python compilation, CLI `--help`, and `git diff --check` passed. Synthetic Chromium review/import and historical recovery journeys passed; the Windows `Offline verification` workflow passed for the exact implementation SHA in both the [push run](https://github.com/MayorofRapture/Recipe-Importer/actions/runs/40126488768) and [PR run](https://github.com/MayorofRapture/Recipe-Importer/actions/runs/40126492197). A synthetic 60 MiB staging probe measured a 2.16 MiB sampled working-set increase at 5 ms intervals; this is one observation, not a guaranteed upper bound.

**3B.2 implementation commits:** `aebd2be44c958a4c6f9d5c5a62454f05dec7ff08` (`Add deterministic cookbook page ranges`) and `fe72d8859757cb3846a73a92482ada78357404f1` (`Bound range PDF temporary output size`), pushed from baseline `f861914b86fb100741f942bb5f8cce9b9d4e2ef5`. Range IDs hash the NUL-delimited version string `recipe-importer:page-range:v1`, parent SHA-256, and canonical first/last page numbers. PDFs live at `parent_documents/ranges/<range_id>.pdf`; a per-range JSON receipt preserves the generated hash and record after a manifest-write failure. The 100-page and 50 MiB per-range defaults are checked before a temporary PDF is written.

**3B.2 verification:** 529 offline tests passed with 1 capability skip (`unittest discover -s tests`, 338.846 seconds, process exit 0); the batch/job subset passed 56/56. Compilation, credential-free CLI `--help`, and `git diff --check` passed. The synthetic Chromium upload/review journeys passed, and the fixture runner passed 9/9. Windows `Offline verification` passed against final implementation SHA `fe72d8859757cb3846a73a92482ada78357404f1` in the [push run](https://github.com/MayorofRapture/Recipe-Importer/actions/runs/40210723147). No live Gemini or Mealie behavior was exercised.

**3B.2 boundary:** page-range creation added no range job, OCR/extraction, candidate/review integration, Mealie import, picker UI, or browser path selection. Same-host native picker integration remains **Unverified** pending a headed Windows proof-of-concept. For remote browsers, a native dialog would select on the server host; any later remote selection needs an authenticated, bounded chunk-to-disk transfer and must never accept a browser-supplied server path. Details and official Streamlit/Tkinter references are in `docs/mealie-compatibility-and-large-pdf-capability.md`.

**3B.3 implementation commit:** `665f14d4fc8526ad74a13e56715db09b4e93ac51` (`Add durable page range extraction jobs`), pushed from baseline `f5c79f37d481f5c3836f8f5e15b80b6a1d16b8f3`. The new `JobService` range APIs persist independent range attempts and job state, recover interrupted work, pause explicitly for user/quota state, and write hash-verified immutable result caches keyed by parent/range identity, selected pages, input bytes, mode, and sanitized extraction configuration. A range must pass parent/range metadata, canonical path, size, PDF, and hash checks before each extraction. Execution requires an explicitly injected extractor and fails closed otherwise; it does not use the ordinary source worker, publish review caches, register candidates, or write Mealie/import/review state. Ordinary file jobs keep their existing manifest and behavior.

**3B.3 verification:** 550 offline tests passed with 1 Windows capability skip (`.venv\Scripts\python.exe -m unittest discover -s tests -v`, 379.272 seconds, process exit 0); the focused job/range subset passed 77/77. Targeted compilation, credential-free CLI `--help`, JavaScript syntax, `git diff --check`, and browser fixture runner tests (9/9) passed. The Windows `Offline verification` workflow passed for exact implementation SHA `665f14d4fc8526ad74a13e56715db09b4e93ac51` in [run 44](https://github.com/MayorofRapture/Recipe-Importer/actions/runs/40218194679). The two synthetic Chromium journeys were attempted locally but could not start because Node received `EPERM` spawning the venv Python; no UI/browser code changed. No live Gemini, Mealie, production state, or production source data was used. Independent read-only code review findings were fixed and covered by regressions. PR #3 remains open and unmerged.

**3B.4 implementation:** `509377f5171c9a89ee143dea164a5843f192bf30` (`Add range candidate review provenance`), pushed from baseline `f67caceac187fb7d1e00803be5c771f68a65e943`. Completed range jobs now register candidates with additive, versioned provenance. Catalog reads independently validate the parent and range evidence and project those registrations without creating them. Range candidates retain original-parent `source_hash`, range/input/extraction/cache provenance, and stable occurrence identity; they are excluded from UI import plans and the CLI backend rejects `range-v1:` identities before contacting Mealie. Retries preserve the original producing job and recover partial append-only registration safely.

**3B.4 verification:** 571 offline tests passed with 1 capability skip (`python -m unittest discover -v`, 419.946 seconds, process exit 0); the focused range/job subset passed 54/54. Targeted compilation, credential-free CLI `--help`, and `git diff --check` passed. The synthetic browser review/import journey passed and verified accepted, edited, rejected, approved, and fake-import states. The separate P0 browser runner failed waiting for its expected `Browser saved manual title` option; this selector timeout remains unresolved. Windows CI passed for the exact implementation SHA in [run 50](https://github.com/MayorofRapture/Recipe-Importer/actions/runs/40225261944). No live Gemini/Mealie calls or writes, production ledgers, scans, or recipe data were used. PR #3 remains open and unmerged.

Phase 3B.4 and Phase 3B.5 are complete under their separately authorized scopes. The Phase 3B.6 compatibility audit is recorded below; this does not authorize the separate UI/UX phase or PR merge.

**3B.4.1 implementation commit:** `ede6390900b973d5faf9b021c3f2c16811156ad7` (`Implement production cookbook range extraction`), pushed from baseline `c132d84df75832599d447f1741bdc0b00fd122c7`. Production range jobs now dispatch through an extraction-only worker for local OCR, Gemini, or OCR-assisted extraction. Cloud consent is persisted and rechecked before each provider request. Requests reserve shared model usage, and quota errors pause the range job. Production results use a separate versioned cache kind with sanitized configuration identity. Candidate registration remains at `register_completed_ranges()`; extraction writes no ordinary extraction/review/import state, and range import protections remain in place.

**3B.4.1 verification:** 578 offline tests passed with 1 capability skip (`.venv\Scripts\python.exe -m unittest discover -s tests -v`, 417.330 seconds, process exit 0); the focused production/UI/model-routing subset passed 56/56. Targeted compilation, credential-free CLI `--help`, and `git diff --check` passed. Browser fixture tests passed 8 with 1 host-capability skip; the synthetic browser review journey passed with screenshots. Upload and P0 browser journeys could not start because the sandbox denied Node's attempt to spawn the venv Python (`EPERM`), so those UI states remain unverified. Windows `Offline verification` passed for the exact implementation SHA in [run 59](https://github.com/MayorofRapture/Recipe-Importer/actions/runs/40231924549). No live Gemini or Mealie calls or writes were made. PR #3 remains open and unmerged.

**3B.5 implementation:** `ae07b1c66be26de97cdb27d898b736148d0a4e5f` (`Add cookbook range review workflow`), pushed from baseline `402a822efde21ae102e0901ea829c201e9d6c7f2`. Upload now discovers parent PDFs from existing batch metadata, previews the original, stages editable inclusive ranges with overlap confirmation and retryable rejection handling, and connects to existing range jobs with cloud consent, progress, pause, and resume controls. Completed range candidates open in Review at their first original page with read-only provenance; approval is available while UI and CLI import remain blocked. Ordinary uploads retain their existing path. The synthetic browser journey covers staged discovery, range commit, local fixture extraction, original-page review, and the import block. The native selector was launched through `run_cookbook_selector.bat` against isolated storage and its real Windows PDF dialog staged a synthetic PDF; screenshots are in local `.test-tmp/browser-evidence/` and are not committed.

**3B.5 verification:** 607 offline tests passed with 2 capability skips (`.venv\Scripts\python.exe -m unittest discover -s tests -v`, 461.273 seconds, process exit 0); the focused cookbook AppTests passed 8/8, including refresh, add/edit/remove, overlap, rejected-range retry, cloud consent, multiple recipe registration, review page mapping, and import exclusion. Compilation, credential-free CLI `--help`, JavaScript syntax checks, and `git diff --check` passed. Fixture-runner tests passed 9/9. Local browser attempts were blocked before Chromium launch when Node received `EPERM` spawning the fixture Python process; the synthetic Chromium journey passed on GitHub Actions Windows CI for the exact SHA in [run 67](https://github.com/MayorofRapture/Recipe-Importer/actions/runs/40242350446). The headed Windows selector proof passed using synthetic data and isolated storage. No live Gemini or Mealie calls or writes were made; production history, scans, and caches were not modified. `.vscode/` and `graphify-out/` were preserved. PR #3 was not merged.

## Phase 3B.6 — Final functional compatibility audit

**Audit date:** 2026-10-25
**Starting/final code SHA:** `c22757d5ddc956f176dbbbb5246c87757fe820da`
**Branch:** `feature/complete-recipe-workspace`
**Overall verdict:** **Conditional Pass** for offline functional compatibility. No application defects were confirmed and no application code or tests were changed. PR #3 remains open and unmerged.

The local branch, upstream-tracking ref, and GitHub PR head all resolved to the requested SHA. The GitHub `Offline verification` workflow for that exact SHA completed successfully in [run 72](https://github.com/MayorofRapture/Recipe-Importer/actions/runs/40245548987). Direct `git ls-remote` was unavailable because this environment could not connect to GitHub through its configured proxy; the connected GitHub repository snapshot supplied the current PR head and CI evidence.

### Compatibility matrix

| Feature/workflow | Evidence reviewed | Tests/checks run | Result | Remaining risks | Required action |
|---|---|---|---|---|---|
| Windows cookbook selection and parent staging | `cookbook_selector.py`, `BatchStore`, prior headed Windows selector evidence, synthetic cookbook browser journey | Full suite; `test_batch_store.py`; `test_cookbook_selector.py`; cookbook Chromium journey | **Passed** | Native file dialog was not relaunched in this audit; prior headed proof uses synthetic data | None for offline gate |
| Immutable parent PDF, stable range identity, original page mapping | `BatchStore` manifest/range validation and `JobService` range identity | Full suite; `test_batch_store.py`, `test_ui_jobs.py` | **Passed** | Provider-side execution is not exactly-once across a crash | Preserve source/hash checks and inspect attempts on retry |
| Multiple recipes, duplicate titles, overlapping ranges | Registration identity and page provenance in `ReviewStore`/catalog | Full suite; `test_range_review_registration.py`, `test_review_identity_authorization.py` | **Passed** | Ambiguous identity remains held for human reconciliation by design | None |
| Durable jobs, reservations, retries, quota pause/resume | Job and production range adapters; persisted usage ledger | Full suite; `test_ui_jobs.py`, `test_production_range_extractor.py`, `test_gemini_provider_accounting.py`, model-routing tests | **Passed** | A provider may finish immediately before a local crash and before its result is durably cached; a retry may repeat the provider request | Treat as provider-request/cost uncertainty; do not claim exactly-once execution |
| Cache integrity and candidate registration recovery | Versioned cache checks, registration append recovery, review projection | Full suite; `test_ui_data.py`, `test_range_review_registration.py`, cache/reconciliation tests | **Passed** | The exact crash between provider response and cache publication is not deterministically injected by the current suite | Add fault injection if the publication sequence changes |
| Cloud consent and repeated UI actions | Durable consent checks, stable job IDs, request reservations | Full suite; `test_streamlit_cookbooks.py`, `test_ui_jobs.py`, provider-accounting tests | **Passed** | No live Gemini behavior was exercised | None for offline gate |
| Historical batch, extraction, draft, review, and CLI histories | Legacy manifest/cache/review fixtures and JSONL projection tests | Full suite; historical-state, identity, review, and import tests | **Passed** | Unlisted user-specific records are not available as fixtures | Preserve append-only histories; no migration was performed |
| Ordinary upload/extraction/review path | Existing Upload, job, catalog, and review path alongside range workflow | Full suite; `test_streamlit_upload.py`, `test_ui_data.py`, `test_ui_jobs.py` | **Passed** in AppTests | Desktop browser journey stopped at a title-field selector; screenshot shows the field and Save draft control. This leaves the browser assertion unresolved, not a reproduced application failure | Recheck browser selector and DOM labeling before using that journey as release evidence |
| Approval and import protection (UI and CLI) | Exact revision/source binding, protected outcomes, ordinary import preflight, range exclusion | Full suite; import-protection, reconciliation, review-identity, and CLI tests; review/P0 Chromium journeys | **Passed** | No live Mealie request or response was tested | Keep range import disabled and empty-instructions gate unchanged |
| Browser workflow and viewport behavior | Synthetic screenshots and rendered control states | Fixture runner **9 passed**; cookbook, review, P0 manual approval, identity, and history-recovery Chromium journeys passed. Upload journey reached Review at 1440px but its selector could not find the visibly rendered title field. At 320px, Upload reflowed vertically; the harness did not expand Streamlit's collapsed navigation to open Review | **Conditional / Unverified** | Complete 320px Review flow and DOM-level keyboard/accessible-name checks were not established | Resolve the browser harness navigation/label query, then repeat desktop and 320px journeys |
| Live Gemini and Mealie integration | Source contract and compatibility notes only | No live calls by instruction | **Unverified** | Installed Mealie behavior, provider model availability, and live quota/network behavior | Requires a separately authorized integration check |
| Full user experience/accessibility | Existing UX artifacts and rendered screenshots | No full accessibility-tree or screen-reader audit in this phase | **Unverified** | Full keyboard/screen-reader conformance, contrast, efficiency, and narrow-screen Review remain unverified | Assess in the separate UI/UX phase |

### Recovery boundary results

- Parent staging interruption cleans partial copies and permits a safe retry; range-PDF generation interruption leaves no published range record or temporary file.
- Durable range job creation is idempotent across repeated starts. Interrupted processing is returned to a retryable state, while completed ranges and quota-paused work remain preserved.
- Cache checksum/configuration/source mismatches fail closed. Partial candidate registration can resume without replacing an existing review revision.
- Draft/review saves and ordinary import intents are append-only and revision-bound; reconciliation rejects changed evidence and protected or uncertain outcomes.
- The suite does not simulate the instant after a remote provider has returned but before the local response cache is published. A retry can repeat that provider call; this does not authorize recipe import and must not be described as exactly once.

### Verification record

- `.venv\Scripts\python.exe -m unittest discover -s tests -v`: **610 tests run; 608 passed; 0 failed; 2 skipped; exit 0** (403.068 seconds). Skips were host capability checks for file and directory symlinks.
- `node --test tests\browser_fixture_runner.test.mjs`: **9 passed; 0 failed; 0 skipped**.
- Synthetic Chromium journeys: cookbook, review, P0 manual approval, identity authorization, and historical recovery passed. The upload journey's DOM selector failed after its Review screenshot; at 320px the harness did not open collapsed navigation. Both outcomes are recorded above as browser-harness gaps pending DOM-level confirmation.
- Python compilation, credential-free `import_recipes.py --help`, JavaScript syntax checks (**10 files**), and `git diff --check`: **passed**.
- No live Gemini or Mealie calls were made. The production `output/` library had no files newer than 2026-10-20 during the audit. `.vscode/` and `graphify-out/` were preserved.
- Graphify's local report/map is stale (built from a different commit); its CLI support file and `networkx` are absent. The map was not rebuilt or modified, and no Graphify conclusion is used as current behavioral evidence.

### Release readiness

- **Offline functional readiness:** Conditional Pass. All 610 Python/AppTests completed with no failures; range/approval/import protections and historical fixtures passed. The browser upload assertion and complete 320px Review journey remain unresolved.
- **Live integration readiness:** Unverified. Gemini and Mealie were intentionally not contacted; range import stays disabled and `MEALIE_SUPPORTS_EMPTY_INSTRUCTIONS=false` remains unchanged.
- **User-experience readiness:** Unverified and deferred. This audit does not establish full accessibility conformance, visual quality, or repetitive-review efficiency.

No functional correction was required, so there is no implementation commit or push for this audit. This roadmap update is documentation-only and remains uncommitted for review.

## Phase 4 — CLI/UI primitive ownership (AUD-004)

**Status:** Deferred maintenance; no refactor in this assignment.

`ui_data.py` and `ui_import_service.py` import `StateStore`, `file_digest`, and candidate identity helpers from `import_recipes.py`; `ui_job_service.py` imports extraction functions from it lazily. The CLI module therefore also acts as a shared library. The audit found coupling, not a current operational failure.

Before any future extraction, document all call sites and persisted ID/ledger contracts for `candidate_identity()`, `_identity_for_title()`, `StateStore`, and `file_digest()`. Demonstrate a concrete maintenance benefit beyond moving code between files. If justified, move only these stable primitives to one narrowly scoped module and re-export them from `import_recipes.py` for compatibility. Keep IDs, import-state JSONL, review events, error handling, and CLI flags unchanged. Add exact historical identity fixtures and run CLI, catalog, job, import-service, and full offline tests. Do not split `ReviewStore` or the Review page as part of this task.

## Separate future UI/UX redesign

The audit and Phase 1 browser journey do not establish that the entire interface is visually effective or efficient. A comprehensive Review redesign remains a separate project. Preserve current function and authority boundaries until a new design scope is approved. A future design should begin with the existing `docs/UI_UX_Audit.md`, `docs/UI_Product_Experience.md`, `docs/UI_Redesign_Roadmap.md`, and project `recipe-review-ux` checklist; it should address repetitive review, source-to-recipe comparison, clear next action, missing detail, and interruption/re-entry. This roadmap does not redesign the UI.

## Safety checklist

- [x] Phase 1 changes are limited to draft feedback and its necessary regression journey/tests.
- [x] Phase 2 source-text review is implemented and verified; Phase 3A remains a specification only.
- [x] Phases 3B.1 through 3B.5 implement parent staging, page-range creation, durable range jobs/cache, range candidate/review provenance, the separate native cookbook selector, and Streamlit range review workflow. Mealie import of range candidates remains disabled; Phase 3B.6 remains gated.
- [x] Phase 4 remains deferred maintenance.
- [x] No live Gemini or Mealie calls were made.
- [x] Production import/review histories, source scans, extraction caches, and recipe records were not modified.
- [x] `.vscode/` and `graphify-out/` were preserved and excluded from commits.
- [x] PR #3 was not merged.

## Phase gates

Phases 1 and 2 and Phases 3B.1 through 3B.5 are complete with passing offline tests, synthetic Chromium journeys where applicable, compilation/help/diff checks, pushed implementation commits, and Windows CI verified at each implementation SHA. Phase 3B.6 is the next independent review and compatibility gate; stop here. Phase 4 remains deferred unless a concrete maintenance benefit and compatibility plan are approved. No phase automatically authorizes the next.
