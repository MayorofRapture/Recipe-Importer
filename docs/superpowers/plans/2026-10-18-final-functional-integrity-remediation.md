# Final Functional Integrity Remediation Plan

> Implement inline as the sole agent. Each behavior change follows a failing-test-first cycle and commits by gate.

**Goal:** Close the remaining F-01–F-08 integrity gaps while preserving import, review, identity, and recovery authority across the CLI and Streamlit app.

**Architecture:** Reuse the existing append-only import and review ledgers, effective identity projection, runtime path safety, and shared UI/service boundaries. Add only the state projection and UI/service seams required for orphaned create investigations, canonical library selection, complete comparison evidence, and stable recovery feedback.

**Tech Stack:** Python 3.14, Streamlit 1.66, `unittest`/AppTest, existing Mealie v3 adapter, isolated Chromium and Node fixture runner.

**Spec:** User attachment `afa8b556-76b4-4549-8f8b-bce6348074d0/Pasted text.txt`.

## Global Constraints

- Never contact Gemini or Mealie; all tests use owned synthetic fixtures and fake services.
- Do not modify production ledgers, source scans, extraction caches, or drafts.
- Preserve append-only history, exact revision approval, identity-link provenance, import protection, tag history, and Windows cross-process locking.
- Keep PR #3 open and unmerged; preserve `.vscode/` and unrelated changes.
- Keep empty-instruction Mealie compatibility disabled and whole-book PDF limits unchanged.

## Review Focus

- An intent without an outcome remains protected and can be inspected/reconciled without retrying.
- Extraction-only mode cannot append any import or tag-repair history, including when an imported candidate has a failed tag.
- Default CLI invocation cannot create recipes; explicit legacy and modern approved import paths remain distinct.
- CLI identity eligibility must match the exact source/hash/extraction effective review projection used by GUI.
- CLI and GUI resolve one absolute library path independent of CWD without moving or merging existing libraries.
- Mapping recovery must reproduce the exact initial target-binding transformation.
- Reconciliation evidence must account for every provider field available and invalidate stale acknowledgments.
- Failed service results remain visible even when optional result identifiers are absent or malformed.

---

## Phase 0: Baseline and reproductions

- [x] Fetch and verify baseline `57272c864c3cc6d1cfeae53057da51e004afe86f` and upstream SHA.
- [x] Run the full offline suite; record counts, skips, and environment (438 tests, 1 skipped; Python 3.14 venv; 303.768 sec).
- [x] Record read-only hashes of protected ledgers; confirm absent files remain absent.
- [x] Inspect existing fixture/browser isolation and map each finding to the current service, UI, CLI, and tests.
- [ ] Add focused failing regressions for remaining Gate B–D issues; F-01, F-03, and F-08 have red/green regressions.

## Gate A: Import safety and interrupted operations

### Task A1: Orphaned create intent projection and investigation

**Files:** `ui_import_service.py`, `app_pages/import_history.py`, `tests/test_ui_import_service.py`, `tests/test_streamlit_import_history.py`.

- [x] Test durable intent with no terminal event, restart, wrong slug, duplicate title, repeated reconciliation, and no second create.
- [x] Project orphaned outcomes separately; contradictory/malformed ledgers remain blocked by existing strict loading.
- [x] Expose exact attempt/source/extraction/revision/digest/snapshot and explicit inspect-then-confirm path; fail closed on search miss.
- [x] Run focused import/AppTest regressions (4 tests); included in Gate A commit below.

### Task A2: Extraction-only import-history isolation

**Files:** `import_recipes.py`, `tests/test_mealie_tags.py`, `tests/test_import_recipes.py` (or existing owning test module).

- [x] Reproduce `--extract-only --batch-tag` on a previously imported candidate with failed tag history, with cached and forced extraction.
- [x] Ensure extraction-only cannot repair tags or call importer; retain extraction cache and review registration behavior.
- [x] Compare import ledger bytes and fake-service calls; verify explicit direct tag repair continues working.
- [x] Run focused tests (`tests.test_mealie_tags`); included in Gate A commit below.

### Task A3: Explicit CLI legacy auto-import opt-in

**Files:** `import_recipes.py`, `README.md`, CLI/import tests.

- [x] Test that default CLI performs no create and rejects contradictory mode combinations.
- [x] Add `--legacy-auto-import`, `--import-approved`, separate failed-tag repair, and execution-mode output.
- [x] Test explicit legacy path, exact modern approval, missing approval, and existing duplicate-protection behavior.
- [x] Update first-run examples; focused tests and CLI help passed; commit Gate A now.

## Gate B: GUI and CLI consistency

### Task B1: Effective identity authority in CLI

**Files:** `import_recipes.py`, `review_store.py` only if existing public projection is insufficient, `tests/test_import_recipes.py`, identity tests.

- [x] Reproduce immutable registration `needs_reconciliation` with a valid link and independently approved second extraction; show CLI/UI disagreement.
- [x] Use the existing exact candidate/source-hash/extraction-ID identity projection; never transfer prior extraction approval.
- [x] Test invalid/unresolved links via identity authorization regressions, eligible linked import, and repeat protection.
- [x] Run focused tests; included in Gate B commit below.

### Task B2: Canonical library path

**Files:** shared path/config module, `import_recipes.py`, `streamlit_app.py`, pages/helpers that currently read output configuration, fixture-path tests, README.

- [x] Trace every path resolution caller and test CWD divergence plus environment override before editing.
- [x] Add one resolver with precedence `--output-dir`, `RECIPE_IMPORTER_OUTPUT_DIR`, stable app-relative default; resolve absolute paths and use it in CLI and GUI.
- [x] Display the active path in CLI and GUI. Detect alternate existing CWD libraries and explain how to select them; never move, merge, or overwrite libraries.
- [x] Test spaces, relative/absolute paths, CWD changes, explicit/env precedence, conflict detection, and fixture-mode isolation.
- [x] Run synthetic Chromium review, approval, and import checks; captured six screenshots under `.test-tmp/browser-evidence/final-integrity`.

## Gate C: Reconciliation correctness

### Task C1: Exact mapping recovery bindings

**Files:** `reconciliation_service.py`, owning reconciliation tests.

- [x] Reproduce a saved selected-field mapping interrupted before lineage; include original field and row suggestions.
- [x] Factor one deterministic selected-section binding transformation used by both first write and recovery; do not loosen equality checks.
- [x] Cover restart/retry with the same operation ID, preexisting revisions, failures before write and after revision/readback/lineage writes, and unchanged proposal authority; cross-process serialization is covered by the existing ReviewStore lock tests.
- [x] Run focused mapping-recovery tests and the full offline suite; committed with Gates C2/D1.

### Task C2: Complete Mealie evidence comparison and confirmation

**Files:** `ui_import_service.py`, `app_pages/import_history.py`, existing Mealie client adapter, import/AppTest tests.

- [x] Inspect actual normalized v3 payload/client response shape; add fixtures for yield, description, and notes availability.
- [x] Compare title, ingredients, instructions, yield, description, and available notes; distinguish formatting-only from material differences and mark unavailable fields explicitly.
- [x] Version the evidence digest and bind reviewer confirmation to the exact displayed Mealie recipe ID and evidence; changed evidence invalidates acknowledgment.
- [x] Cover wrong ID, unavailable fields, differences, external edits with rationale, reload/repeat, and unchanged no-duplicate protections.
- [x] Run focused service/AppTest tests and the full offline suite; committed with Gates C1/D1.

## Gate D: Recovery feedback

### Task D1: Display all reconciliation outcomes

**Files:** import-history action state/service result, `app_pages/import_history.py`, `tests/test_streamlit_import_history.py`.

- [x] Reproduce service failures without returned attempt/candidate IDs and confirm they disappear in UI.
- [x] Key each request by stable local action identity; persist success/failure/reason/protection/next-step feedback across rerenders until dismissed/replaced.
- [x] Cover malformed history, invalid slug, provider errors, revision/evidence mismatch, repeat confirmation, search/page change, app restart, and tag-repair feedback.
- [x] Run AppTest and owned browser recovery path; committed with Gates C1/C2.

## Gate E: Cross-interface acceptance and delivery

- [x] Complete synthetic journeys for safe first-time use, explicit legacy mode, identity reconciliation, orphaned intent recovery, mapping recovery, and extraction-only isolation through service/AppTest coverage and three synthetic Chromium flows.
- [x] Run Windows cross-process synchronization tests, full and clean-checkout offline suites (463 tests, 1 skipped each), AppTest, three isolated Chromium flows, Node harness syntax checks, targeted Python compilation, CLI help, `pip check`, and `git diff --check`.
- [x] Recheck protected hashes and absent-file state. All three existing production ledger hashes match baseline; `output/ui_import_events.jsonl` remains absent.
- [x] Commit logical gates without rewriting history and push `feature/complete-recipe-workspace`; push and PR GitHub Actions passed on implementation SHA `a1ca5ea` (run IDs `39152918066` and `39152922726`).
- [x] Keep PR #3 open and unmerged; no live Mealie/Gemini behavior was exercised or claimed.
