# Comprehensive Functional Correctness Remediation Implementation Plan

> **For agentic workers:** Use the executing-plans workflow task by task. Each behavior change starts with a focused failing test and ends with focused verification.

**Goal:** Make recipe review, extraction retry, uncertain-import reconciliation, and recovery report reliable without losing work, weakening authorization, or duplicating recipes.

**Architecture:** Preserve the current Streamlit, JSONL, and service boundaries. Fix state synchronization at the shared editor refresh boundary, retry jobs by durable source outcome, derive uncertain import identity from validated event chains, and make multi-event retry mappings safely idempotent. Keep external Mealie/Gemini behavior mocked; leave empty-instruction support gated and assess whole-book PDFs separately from recipe-sized uploads.

**Tech Stack:** Python 3.14, Streamlit 1.66, unittest/AppTest, Node fixture tests, existing JSONL stores, and the owned synthetic Chromium runner.

**Spec:** User attachment `bc26c63c-ce86-4446-9a43-8b3719088a87/Pasted text.txt` (2026-10-18).

## Global Constraints

- Never write to production ledgers, drafts, extraction caches, source scans, or Mealie.
- Preserve append-only history, exact source/extraction/revision/content authorization, import idempotency, and uncertain-create protection.
- Do not perform live Gemini requests or Mealie writes; do not merge PR #3.
- Work on `feature/complete-recipe-workspace`; preserve pre-existing `.vscode/`.
- Keep existing CLI and legacy JSONL compatibility; no destructive migration.
- Empty-instruction imports remain disabled until installed Mealie behavior is verified and explicitly authorized.

## Review Focus

- A proposal decision can conflict with unsaved scalar editor values or stale widget state.
- A failed job must retry only incomplete sources while excluding any live worker lease.
- An uncertain attempt must be reconstructed from unambiguous durable events even without compatibility state.
- Same-title Mealie recipes require explicit content comparison and reviewer evidence.
- A UI recovery result must remain visible after rerender and report the service's actual outcome.
- Reconciliation retries must finish partial writes without creating duplicate revisions or candidate registrations.
- Resource and provider-compatibility limits must remain conservative when external behavior is unavailable.

---

## Baseline and finding investigation

- [x] Confirm baseline commit `98b8724f341761824ff8b5dc3f5a361c89eb4784`, synchronized branch, and unrelated `.vscode/`.
- [x] Run `.venv\Scripts\python.exe -m unittest discover -s tests`: 419 passed, 1 capability skip.
- [x] Record read-only hashes for review/import/draft ledgers; preserve missing `ui_import_events.jsonl` as missing.
- [ ] Add focused reproductions for F1–F7 before changing behavior; classify each finding using the requested status labels.
- [ ] Inspect installed-version configuration and mocks for F8; do not use live Mealie.
- [ ] Assess F9 limits, page-range provenance, identity, and overlap behavior; implement only if it fits existing contracts without schema redesign.

## Task 1: F1 proposal/editor synchronization

**Files:** `app_pages/common.py`, `app_pages/review.py`, `tests/test_review_buffers.py`, `tests/test_streamlit_structured_review.py`, browser acceptance harness if needed.

- [x] Add failing AppTest coverage for accepted title, yield, and description proposals through rerender, tab switch, second save, and a fresh app/reloaded revision.
- [x] Prove unrelated unsaved scalar edits remain dirty and visible after a refresh; existing proposal decision tests cover unchanged rejection behavior.
- [x] Fix the shared post-decision refresh to synchronize authoritative scalar values and widget keys without discarding independent unsaved edits.
- [x] Verify each accepted value is durably loaded at revision one after restart; broader approval/content binding remains covered by existing review tests.

## Task 2: F5 first-revision manual approval

**Files:** `app_pages/review.py`, `tests/test_review_navigation.py`, `tests/test_review_authority.py`.

- [x] Reproduce the revision-zero manual-approval form; existing form exposed acknowledgment before any durable recipe revision.
- [x] Require an explicit save/reload of revision one before showing usable manual-approval controls.
- [x] Verify acknowledgment/rationale bind to revision and digest, reset after edits, and stale concurrent approval is blocked by the existing exact revision revalidation.

## Task 3: F2 failed extraction-job retry

**Files:** `ui_job_service.py`, `app_pages/upload.py`, `tests/test_ui_jobs.py`, `tests/test_streamlit_upload.py` if present.

- [ ] Reproduce worker-launch failure followed by `resume`, failed worker initialization, interrupted work, and partial-source failure.
- [ ] Add explicit retry for failed jobs that requeues only unresolved sources; retain previous attempts and successful results/caches.
- [ ] Keep the existing job/global worker leases authoritative across app restarts and sessions; test duplicate launch rejection.
- [ ] Show the failure reason, completed/unfinished counts, preservation notice, and a retry action without another upload batch.

## Task 4: F3/F4 uncertain import evidence and confirmation

**Files:** `ui_import_service.py`, `app_pages/import_history.py`, `tests/test_ui_import_service.py`, `tests/test_streamlit_import_history.py`, import history fixture helpers.

- [ ] Reproduce uncertain creation with no compatibility state and show current title lookup loses the intended recipe identity.
- [ ] Add a validated per-attempt event projection for intent, create outcome, immutable approved recipe snapshot, attempt/source/revision/digest/title/tag, and compatibility state.
- [ ] Fail closed on missing/duplicate/contradictory history; test incorrect slug, missing ID, wrong attempt, and repeated reconciliation.
- [ ] Replace title-only linking with side-by-side local/provider recipe comparison and explicit confirmation. Show material ingredient/instruction differences and require stronger evidence for them.
- [ ] Test no second create after reconciliation, durable event append, reload persistence, and protected state.
- [ ] Keep Mealie representations realistic and mocked; do not infer absence from a slug lookup miss.

## Task 5: F6 durable recovery result presentation

**Files:** `app_pages/import_history.py`, `tests/test_streamlit_import_history.py`, isolated browser harness.

- [ ] Add AppTest reproductions for failed reconciliation and failed tag repair currently disappearing after rerun.
- [ ] Persist structured outcomes in session state until dismissed or replaced; distinguish verified success, verification failure, unresolved absence, tag repair success/failure, uncertainty, and local-history repair.
- [ ] Preserve search/page/selected item and assert the UI never reports success from a non-throwing failure result.
- [ ] Verify true fake-service reconciliation and tag repair feedback.

## Task 6: F7 recoverable retry-mapping writes

**Files:** `reconciliation_service.py`, `review_store.py` only if required, `tests/test_reconciliation.py`, `tests/test_review_reconciliation.py`.

- [ ] Add fault injection before first write, after first durable write, during second write/readback, before termination, and on retry after partial completion.
- [ ] Reproduce duplicate/rejected mapping retry and identify whether existing operation IDs already recover each write.
- [ ] Implement the smallest idempotent recovery boundary; reuse operation IDs and verify exact revision content, parent, source, extraction lineage, identity, and import protection before finishing a partial mapping.
- [ ] Verify concurrent retries, restart visibility, no duplicate revisions/candidates, and legacy history compatibility.

## Task 7: F8 compatibility gate and F9 capability assessment

**Files:** tests for mock Mealie response contract; `README.md` or a focused capability plan if findings require documentation.

- [ ] Verify F8's current disabled gate and inspect only supplied version/configuration and existing API mocks. If installed behavior cannot be established read-only, keep the gate disabled and provide a disposable-recipe acceptance procedure; do not request a live write in this task.
- [ ] Add version-targeted mock contract tests for empty `recipeInstructions`, response shape, rejection/default-step cases; label live compatibility blocked.
- [ ] Assess F9's 50 MiB/100-page bounds and existing extraction/render pipeline. If page ranges require new lineage, identity, or overlap schema, do not force implementation; write a concrete remaining-capability design and test the current safe rejection.

## Task 8: End-to-end verification and delivery

- [ ] Complete owned synthetic happy path and recovery path across upload, extraction retry, edit/proposal, saved revision, approval, preview, fake import, duplicate prevention, uncertain reconciliation, tag-only retry, and interrupted mapping recovery.
- [ ] Run focused tests, full unittest suite, AppTest/browser flow, Node fixture tests, compile, CLI help, `pip check`, and `git diff --check`.
- [ ] Run a clean-checkout suite and GitHub Actions for the pushed commit; report no live-service verification unless explicitly performed.
- [ ] Recheck production ledger hashes and missing-file state.
- [ ] Commit each coherent functional correction and push the existing feature branch without rewriting history or merging PR #3.
