# D3 Upload and Import/History Refinements Implementation Plan

> **For agentic workers:** This plan is executed inline, task by task. Each task is independently testable.

**Goal:** Make Upload easier to resume and separate eligible imports from protected history without changing job, review, import, or reconciliation authority.

**Architecture:** Reorder the existing Streamlit page presentation around the current batch and current eligibility. Keep all operations routed through `BatchStore`, `JobService`, `ImportService.preview()`, and `ImportService.dispatch()`; retain current session-state identities and scope cloud consent to its source/configuration context.

**Tech Stack:** Python, Streamlit 1.66.0, AppTest, existing local Chromium/CDP fixtures.

**Spec:** `docs/UI_Redesign_Roadmap.md`, `docs/UI_Product_Experience.md`, and D3 request supplied by the user.

## Global Constraints

- Preserve the three workspaces and all existing selection/order semantics.
- Do not alter extraction or Mealie import logic, exact-revision eligibility, attempt identity, or reconciliation protection.
- Keep cookbook range candidates excluded from import and show the exclusion before the final action.
- Use isolated synthetic data and mocked services; do not contact Gemini or Mealie.
- Preserve `.vscode/`, `graphify-out/`, `D2-review.patch`, production data, caches, and ledgers.

## Review Focus

- Cloud consent cleared after batch, source, or extraction-mode changes; test session state and browser flow.
- Partial upload retains independent accepted/rejected outcomes; test AppTest and browser flow.
- Existing paused and quota-paused jobs remain visible with safe resume controls; test existing service fixtures.
- Import plan is invalidated by selection/tag changes and exact revisions remain service-validated; test AppTest.
- Uncertain creation stays protected and range candidates remain excluded; test AppTest and browser flow.

---

### Task 1: Upload hierarchy and consent scope

**Files:**
- Modify: `app_pages/upload.py`
- Test: `tests/test_streamlit_upload.py`
- Update: `docs/wireframes/ui-redesign/D3-upload-import-history.md`

**Interfaces:** Keep `BatchStore`, `_job_service`, `JobService`, `_stage_selected_files`, `_render_job_panel`, and `_render_cookbook_workflow` interfaces unchanged.

- [ ] Add AppTests for first-use batch creation, returning batch status/selection, partial upload, resume visibility, and consent reset after source scope changes.
- [ ] Run the focused Upload AppTests and confirm new behavior assertions fail before implementation.
- [ ] Reorder existing controls to show selected batch/status, selector/recent context, current job, per-file results, upload/extraction setup, and cookbook ranges; keep creation prominent only when there are no batches.
- [ ] Reset ordinary cloud consent when the selected batch, staged source identities, or extraction mode changes.
- [ ] Run focused Upload AppTests.

### Task 2: Import and History separation

**Files:**
- Modify: `app_pages/import_history.py`
- Test: `tests/test_streamlit_import_history.py`
- Update: `docs/wireframes/ui-redesign/D3-upload-import-history.md`

**Interfaces:** Keep `ImportService.preview()`, `dispatch()`, `history()`, reconciliation, and tag repair operations unchanged.

- [ ] Add AppTests for separated Ready/History sections, distinct creation/tag outcomes, protected uncertain records, and range exclusion before final import.
- [ ] Run focused Import/History AppTests and confirm new presentation assertions fail before implementation.
- [ ] Put recovery history behind a clear secondary disclosure, show human-readable outcome classes, and state creation and tagging results separately.
- [ ] Preserve search, pagination, attempt-bound reconciliation controls, and selection/preview signatures.
- [ ] Run focused Import/History AppTests.

### Task 3: D3 verification and delivery

**Files:**
- Update: `docs/UI_Redesign_Roadmap.md`
- Update: `docs/wireframes/ui-redesign/D3-upload-import-history.md`
- Add sanitized browser evidence under: `docs/wireframes/ui-redesign/evidence/`

- [ ] Run the complete offline suite, browser fixture tests, Python compilation, CLI help, JavaScript syntax checks, and `git diff --check`.
- [ ] Run synthetic Chromium checks at 1440x900 and 320x900 for Upload, Import/History, and the existing Review journeys.
- [ ] Record exact outputs, screenshots, confirmed defects, and accessibility/integration limits.
- [ ] Commit the focused Upload and Import/History changes separately, then push and verify Windows CI against the final SHA.
- [ ] Stop for independent review; do not begin D4.
