# Recipe-Importer UI redesign implementation plan

> For Luna: use the installed `executing-plans` workflow task by task after user approval. This is a design handoff, not authorization to begin implementation, contact services, commit, push or merge. Work on the existing development branch; preserve unrelated changes.

**Goal:** Deliver a dependable source-to-Mealie workspace that makes repetitive review comfortable and preserves exact human consent and recovery.

**Architecture:** Streamlit pages present view models and dispatch narrow commands. Existing Python extraction, review, payload, identity, routing and Mealie code remains the owner of business behavior. Add small boundary services only where current UI/CLI projections disagree or a job lifecycle does not exist; no parallel importer or frontend framework.

**Tech stack:** Python, currently installed Streamlit 1.66.0, Pydantic, PyMuPDF/Pillow, native Streamlit controls, unittest/AppTest; Playwright for rendered verification when available. No new dependency assumed.

**Spec:** [Product experience](UI_Product_Experience.md), [audit](UI_UX_Audit.md), [wireframe index](wireframes/ui-redesign/README.md). These supersede old layout/default-mode choices only after approval; preserve old files as history.

## Global constraints

- No pending proposal becomes authoritative/importable without recorded human Use/Edit. Never label approved missing-source content as transcription.
- Keep save, approve, create and tag outcomes separate. Imported and uncertain entries remain protected.
- Preserve IDs, sources, caches, drafts, review/import ledgers and existing CLI behavior except the explicitly documented approval/projection correction. No destructive migration.
- No live services in automated tests. Explicit authorization and installed API validation precede live tests. No `.env`, private scans or output artifacts in commits/screenshots.
- Use actual source hash + original candidate identity, not array position or edited title. Ambiguous split/merge/rename reconciliation blocks new creation.
- Native Streamlit first; targeted components/CSS only after a documented native feasibility check. Test actual browser DOM, focus and reflow; string assertions are insufficient.
- All commands here are PowerShell-compatible. Use `& .\.venv\Scripts\python.exe`; do not substitute POSIX activation. No task includes a commit command.

## Review focus

1. Draft content, proposal decisions and approval revision must agree across UI and CLI (R1 tests).
2. Focused text, mode conversion, reorder/delete and changing filters must survive reruns/navigation without moving a proposal to another field (R1/R2 tests).
3. Missing caches, malformed tails and multiple local writer instances must not hide protected import outcomes or silently overwrite newer work (R1/R3/R5 tests).
4. Unknown recipe counts, quota pauses and in-flight work must not be presented as completion (R4/R5 tests).
5. Started-but-unconfirmed import or tag operations after crash must resume through reconciliation/repair, never new recipe POSTs (R3 tests).

## Sequence and checkpoints

| Milestone | User value | Dependency | Inspect before proceeding |
| --- | --- | --- | --- |
| R1 Review authority and editing correctness | Saved decisions mean what the user thinks they mean | Design approval | Exact-revision approval, UI→CLI payload, reorder and proposal regression evidence |
| R2 Review workspace | Fast, readable source comparison and correction | R1 | Actual 1440×900 UI, Caramel bulk cleanup, keyboard/recovery; user inspects screenshots |
| R3 Import & history | Existing reviewed recipes can reach Mealie without CLI | R1 + approved R2 checkpoint | Mock creation/tag/uncertain/crash walkthrough; live compatibility remains separate |
| R4 Source intake and extraction | First-time/batch users can add and process scans | R1–R3 | Synthetic uploads, pause/quota/restart and multi-source progress |
| R5 Re-extraction and identity reconciliation | Difficult sources recover without losing imported siblings or edits | R1, R3, R4 | Split/rename/duplicate ambiguity and draft-vs-extraction comparison |
| R6 Product validation and scaling | Reliable repeated use across large collections and widths | R2–R5 | End-to-end journeys, performance/accessibility report and remaining limitations |

R1 and R2 are deliberately separate. Correctness can be accepted even if the proposed layout needs revision. Upload follows import to make existing cached work useful earlier. R5 separates consequential re-extraction from ordinary quota resume.

## Module ownership map

| Existing module | Preserve/reuse | Expected change |
| --- | --- | --- |
| `recipe_quality.py` | Candidate/diagnostic/proposal/schema payload types | Shared effective-review eligibility contract; no inferred quantity/unit parsing. |
| `review_store.py` | Append-only human decisions; original proposal retained | Additive snapshot/resolution/lineage events and backward-compatible reader, if needed. |
| `draft_store.py` | Durable append-only drafts and raw text | Revision expectations, row/source bindings and conflict-safe writes; preserve legacy records. |
| `ui_data.py` | Read-only catalog/source renderer | Shared authoritative projection, separate sources/history, disclosed errors and revision-aware caching. |
| `ui_review_state.py`, `quick_edit.py` | Row transforms and line/block codec | Stable row IDs, target bindings, explicit bulk Apply/Cancel/undo semantics. |
| `app_pages/common.py` | Paths, buffer and persistence orchestration | Use shared review commands, dirty guards and accurate saved-state feedback. |
| `app_pages/review.py` | Active recipe presentation | Replace accumulated queue/actions/row controls with wireframed workspace. |
| `streamlit_app.py` | Native page navigation | App-wide context/resume and safe navigation policy. |
| `import_recipes.py` | Digest, identity, StateStore, processing/routing and CLI | Consume exact effective approval; expose narrow selected import/extract boundaries without copying processing logic. |
| `mealie_client.py` | Existing create/tag API expectations | Reuse; reconcile only through validated installed contracts. |
| `model_routing.py`, `local_ocr.py`, `ocr_structuring.py` | Quota, OCR evidence and line-reference behavior | UI adapters only unless a named lifecycle gap demands a bounded change. |
| `app_pages/import_history.py`, `app_pages/upload.py` | Existing destinations | Replace placeholders in R3/R4. |

Proposed new modules below are bounded recommendations, not scaffolding requirements. If an existing owner can contain the function clearly, keep it there and update handoff paths. Do not create a generic repository/service/controller hierarchy.

## Common execution and verification protocol

For every task: (1) write the named failing tests; (2) run the focused unittest module and confirm the expected failure; (3) implement the smallest correction; (4) rerun focused tests; (5) inspect the relevant actual UI where presentation changed. A failure is not permission to change payload/source semantics. Task tests must use temporary output/source roots, fake network boundaries and synthetic scan/text content.

At each milestone run:

```powershell
& .\.venv\Scripts\python.exe -m unittest discover -v
& .\.venv\Scripts\python.exe -m compileall -q streamlit_app.py app_pages ui_data.py ui_contracts.py ui_review_state.py draft_store.py review_store.py recipe_quality.py import_recipes.py tests
& .\.venv\Scripts\python.exe import_recipes.py --help
git diff --check
git status --short
```

Add new milestone modules to the targeted syntax command. Record actual counts, exit codes and limitations. Never call a browser check Verified merely because Streamlit health is HTTP 200. Check for no `.env`/production `output/`/source changes. Stop for user inspection at each checkpoint; implementation and verification are reviewable before any new permission request.

Browser artifacts go into a sanitized temporary directory outside production output, then copy approved sanitized evidence into the milestone evidence folder under `docs/wireframes/ui-redesign/evidence/`. Do not use the Playwright skill's ordinary `output/playwright/` location when it could touch valuable production output. This explicit fixture-only boundary takes precedence. Screenshots and traces must exclude private content/tokens.

## R1 — Review authority and editing correctness

### Objective, scope and dependencies

Fix A01–A03/A07 before visual work. The user can trust that accepted values persist, approved content is exact, and reorder changes the intended entry. Dependency: user approves this plan and the additive review semantics. No extraction or live import, no new layout, no ledger rewrite.

R1.1 regression fixtures and the approved additive event/identity/concurrency contract are tracked in [the R1.2 contract](UI_R1_2_Approval_Revision_Contract_Proposal.md). R1.2 records its implementation and compatibility boundaries there; R1.3 remains a separate milestone.

### Task R1.1: Establish failing orchestration fixtures

**Files:** modify `tests/ui_fixture_factory.py`, `tests/test_streamlit_structured_review.py`, `tests/test_streamlit_responsive_review.py` and `tests/test_ui_data.py`; add `tests/test_review_revision.py`.

- [ ] Add source-consistent synthetic image/PDF text for Fried Rice, Caramel Apples and full-title Cranberry & Caramel Date Bars. Keep existing abbreviated fixture compatibility; private scans are not fixtures.
- [ ] Add tests `test_structured_reorder_survives_widget_rerun`, `test_structured_remove_then_edit_targets_remaining_row`, and `test_all_proposals_for_a_field_and_unindexed_proposal_are_rendered`.
- [ ] Add `test_accept_suggestion_after_saved_draft_survives_reload`, `test_content_changed_after_approval_is_not_import_ready`, `test_ui_approved_draft_is_the_cli_import_payload`, and `test_empty_recipe_is_not_approved`.
- [ ] Add duplicate-title identity, review-approval isolation, and import-history isolation tests for two distinct Fried Rice candidates in one source.
- [ ] Exercise approval through AppTest and assert it can advance without writing to an already-instantiated selector.
- [ ] Reproduce current failures using `python -m unittest tests.test_review_revision tests.test_streamlit_structured_review tests.test_review_import -v`; distinguish new regression failures from existing passes.

### Task R1.2: One effective-review projection and exact consent

**Files:** `review_store.py`, `recipe_quality.py`, `ui_data.py`, `app_pages/common.py`, `import_recipes.py`; propose `review_service.py` only for the shared boundary if it keeps page/CLI code smaller.

**Proposed boundary:** `load_effective_review(output_dir: Path, candidate_id: str) -> EffectiveReview`, `save_review_draft(candidate_id: str, expected_revision: int, content: RecipeCandidate, row_bindings: dict) -> SavedDraft`, `approve_review(candidate_id: str, expected_revision: int) -> ApprovalResult`. Return types carry content, original identity, extraction lineage, revision, issue resolutions, protection and explicit success/conflict/failure reasons. No API calls inside these functions.

**Data flow:** immutable extraction → valid decisions bound to that extraction → nonconflicting draft field edits → issue resolutions → exact approval snapshot. Do not replace decision metadata with a draft blob. Distinguish dirty editable content from approved snapshot. The CLI and UI consume the same effective projection for any candidate with UI review events.

- [ ] Specify additive events: approval snapshot with content digest/revision/lineage; field resolution with original target and reason; existing `candidate_decision` values remain readable. Preserve original suggestion and final human value.
- [ ] Keep legacy CLI `approved`/`edited` meanings when no newer UI draft conflicts; old approval does not authorize a later draft. Treat ambiguous legacy approval/draft chronology as Needs attention, never guess.
- [ ] Share completeness, pending-proposal and unresolved-critical checks. Rejecting a proposal never erases its critical diagnosis. Optional absence does not block. New approval requires inspectable original source; an unavailable preview blocks new approval while unchanged previously approved snapshots retain their consent.
- [ ] Freeze candidate ID through title edits. Lookup the original ID when importing approved renamed content; do not recompute it from edited title. Re-extraction mismatches are held for R5.
- [ ] Approval saves/readbacks exact revision before consent, then verifies newly written consent matches that revision. If save succeeded but approval failed show **Draft saved; approval not recorded**, keep selection and retry only consent after revalidation.
- [ ] Test pending/approved/edited/rejected proposals, optional omissions, both overlapping draft/decision edits and nonoverlapping merges, approval followed by content edit, critical source correction, human completion provenance, and legacy state fixtures. Test explicit evidence-backed **Not recipe content** resolution for removed OCR artifacts: raw line/diagnosis retained, ordinary deletion cannot resolve it, unrelated critical findings still block. Test missing source blocks new approval, hash-matching relink enables it, and unchanged old approval is retained.

### Task R1.3: Stable rows, buffers and conflict boundaries

**Files:** `ui_review_state.py`, `app_pages/common.py`, `draft_store.py`, `app_pages/review.py`; tests in `tests/test_ui_review_state.py`, `tests/test_draft_store.py`, `tests/test_review_revision.py`.

**Responsibilities:** Row IDs identify editor entries; extraction field paths identify source targets. Draft revisions store both without changing candidate identity or cache. Bind every structural mutation and proposal decision to expected revision; rebuild widget keys only after committed buffer capture, never by new positions.

- [ ] Stable UI row IDs survive insertion/removal/reordering and saved reload. A deleted target goes to unplaced issues rather than following an index. Multiple proposal IDs remain distinct.
- [ ] Refresh buffers after review decisions using shared projection; detect dirty same-field conflict rather than overwriting it. Never mutate instantiated widget state in the wrong phase.
- [ ] Expected-revision write checks occur under a shared local writer boundary. Per-instance `threading.Lock` is insufficient. Use the smallest reviewed single-writer/lock approach compatible with CLI; if reliable cross-process exclusion cannot be established, refuse concurrent mutation with actionable copy.
- [ ] Preserve legacy drafts by deriving row IDs at load and persisting on next explicit save; no bulk migration. Detect malformed trailing records and disclose safe partial reads without erasing history. Corrupt import protection remains a block.
- [ ] Test two tab/store instances saving same revision: one succeeds, second retains buffer and receives conflict; read-only inspection remains available. Test crash/incomplete append recovery against backup copies.

**Accessibility:** existing control names retained; new failure/conflict text includes next action, no color-only cue. **Automated acceptance:** all named regressions pass at actual page/CLI boundary; pending proposals absent from payload; successful old identities unchanged. **Browser evidence:** if available, reorder/edit and failed-save recovery with screenshots; if unavailable, R1 correctness can be accepted from deterministic tests but R2 usability gate remains blocked.

**Definition of done / handoff:** effective-review event contract and legacy matrix documented, named regressions/full suite passing, exact edited payload shown via fake client, no production writes; user inspects R1 evidence before layout work. Risks: event compatibility, existing title identity, local concurrency. Excluded: auto-clearing critical issues, approving all recipes, live reconciliation, database migration.

## R2 — The review workspace

### Objective and scope

Replace navigation clutter and wide row control grids with a purposeful two-pane document workspace. Review both ordinary correction and extensive cleanup. Dependency: R1 approval and [review wireframes](wireframes/ui-redesign/02-review.md). No import/upload backend, no model changes.

### Task R2.1: Context, queue and visual foundation

**Files:** `streamlit_app.py`, `app_pages/review.py`, `ui_contracts.py`, `ui_data.py`; create `.streamlit/config.toml` only for native theme values; extend `tests/test_streamlit_navigation.py`.

- [ ] Retain three top destinations. Render one compact context row, recipe header and action strip. Existing library/source filename before R4 batch manifests; never source-hash Batch labels.
- [ ] Add Queue drawer/dialog with search, presets, source filter and paginated rows (initial 25/page); one active editor. Display same-title candidates with source/page where available.
- [ ] Store queue snapshot IDs and current position while advancing; filter changes preserve selected identity if eligible, otherwise choose first with an explanation. At end show reviewed/deferred/ready counts. No accidental wrap or disappearing selected item during autosave.
- [ ] Theme implements product tokens using native configuration, not global DOM selector CSS. Do not fetch external fonts or add decorative graphics.
- [ ] Tests: filter/no-match/end-of-queue, previous from end, protected inspection, identity retained across page changes; no unrelated page body rendered. Search matches title/source filename; page length bounded.

### Task R2.2: Source comparison and editing ergonomics

**Files:** `app_pages/review.py`, `ui_data.py`, `ui_review_state.py`, `app_pages/common.py`; tests `tests/test_streamlit_structured_review.py`, `tests/test_quick_edit.py`, `tests/test_ui_data.py`.

- [ ] Compose two ~45/55 panes, readable row/step editors and supported title/yield/description fields. Optional metadata is collapsed and labeled optional. Do not add unsupported prep/time fields.
- [ ] Replace permanent move/delete grids with native labeled row menu. Implement add, move, remove, merge steps and Undo last edit. Split via explicit Bulk blocks unless a tested cursor-aware component exists.
- [ ] Add section-level Bulk edit Apply/Cancel, structure/count preview and raw draft restore. Preserve suspicious lines and Unicode/fractions. Changing mode cannot auto-approve or silently remove artifacts.
- [ ] Show issue summary plus complete Issues view; indexed proposals beside bound rows, nonindexed/unplaced proposals included, multiple proposals per field all inspectable. Use/Edit writes through R1 with exact current revision.
- [ ] Bound page selector to actual PDF length; separate recipe/page movement; display unavailable-source recovery. Cache preview by hash/page/scale with memory bound; maintain original-text access and last page.
- [ ] Run a native feasibility spike for fit/zoom (e.g., wider source dialog/native viewer or scale controls). Select smallest option meeting quantity-reading and keyboard exit-to-fit tests. Record native limitation before custom pan/zoom component; do not implement invented source highlights.
- [ ] Test Caramel bulk removes 15 artifact lines only on Apply; Cancel/refresh restores raw draft, 12 fragments→5 explicit blocks, proposal target preserved/held after merge, source page out-of-range disabled.

### Task R2.3: Saving, navigation and actions

**Files:** `app_pages/common.py`, `streamlit_app.py`, `app_pages/review.py`; add `tests/test_review_navigation_recovery.py`.

**Flow:** widget commit → in-memory content revision → guarded durable save/readback → saved cue. Approval calls R1 exact-consent boundary. Navigation calls the same save guard before changing page/queue/selection. Save failure and conflicts keep current content visible.

- [ ] Show Saved locally/Saving/Unsaved text/Save failed/Draft restored. Save committed native widget changes; never claim every keystroke is durable. Persist resume pointer with candidate ID, queue/batch and source page after successful save.
- [ ] Approve & next primary; Save, Previous and Skip secondary. Save & next remains a secondary menu action for compatibility, not a fifth equal primary button. Skip saves and defers without rejecting/approving.
- [ ] Guard previous/next/filter/queue/top-nav exits, including focused unsent text. If native top navigation bypasses a reliable guard, settle a scoped component or explicit safe-save navigation fallback at this checkpoint. Do not ship a silent-loss path with “autosave” copy.
- [ ] Sticky action strip is a desired browser behavior, not an assumption about `st.container`; compare a small app-scoped CSS solution against duplicated native top/bottom actions. If sticky focus/viewport tests fail, choose native fallback and show actions near each section. Document the chosen fallback in milestone evidence.
- [ ] Tests: failed save/approval no advance, draft success/approval failure separate feedback, proposal conflict retains text, skip persists but not import-ready, mode switch/navigation preserves draft.

**Accessibility:** visible named fields/menu actions, keyboard reorder alternatives, focus retained after edits and moved deliberately after next; status announcements; 320px/200% reflow; actions do not obscure focus. No table-style instruction editor or icon-only delete controls.

**Browser verification:** start app only against sanitized fixture directories. Walk Fried Rice happy path and failed-save recovery. Capture `r2-review-1440x900`, `r2-bulk-caramel`, `r2-critical-proposal`, `r2-save-failed`, `r2-queue-end`, `r2-review-1366x768`, `r2-reflow-320`, `r2-text-200pct`, `r2-focused-actions`. Inspect source readability, pane widths, action reachability, focus and DOM labels. A stub PDF alone is inadequate; synthetic source text must visibly match expected corrections. Run scoped accessibility audit and list criteria unverified from each input.

**Definition of done / handoff:** goals in product experience exercised; user inspects layout and correction ergonomics before R3. Document actual action counts, native/component decisions and evidence. Tests alone cannot approve this milestone if browser unavailable. Risks: focused-input commit, widget lifecycle, sticky scrolling and preview memory. Excluded: shortcuts, drag reorder, OCR coordinate fabrication, automatic cleanup, forced always-visible third column.

## R3 — Selected import and independent history

### Objective and scope

Let existing approved content reach Mealie through a safe UI. Show tag repair and uncertainty separately. Dependencies: R1 exact consent, accepted R2 review, installed API contract check before live testing. [Import wireframe](wireframes/ui-redesign/03-import-history.md).

### Task R3.1: Narrow import preflight and service

**Files:** `app_pages/import_history.py`, `ui_data.py`, `import_recipes.py`, `mealie_client.py`; propose `ui_import_service.py` for selected commands; tests `tests/test_ui_import_service.py`, `tests/test_review_import.py`.

**Proposed interface:** `preview_import(candidate_ids: list[str], batch_tag: str) -> ImportPlan`; `dispatch_import(plan_id: str) -> ImportOperation`. Plan freezes identities/revisions/tag intent, displays eligible/excluded counts, then rechecks protection/content before each dispatch. Operation exposes per-recipe intent/create/tag outcome and reads existing clients/state. No extraction, sibling processing or payload building in the page.

- [ ] Default selection empty; Select all eligible explicit. Review count/tag/destination and **Import n recipes** before dispatch. Serialize a single local operation; double-click/rerun cannot enqueue twice.
- [ ] Use R1 approved content and `schema_recipe`/existing create function; do not call `process_file` wholesale. Legacy complete-but-unreviewed CLI candidates retain CLI compatibility but UI requires explicit review.
- [ ] Record local operation intent before POST. On confirmed slug, persist creation immediately before tagging; do not delay creation record until tag call. After crash, started-but-unconfirmed creation is Check Mealie, not retryable.
- [ ] Revalidate selection immediately before each side effect; cancel further dispatch after protection/consent changes. Failed ledger write after server success is uncertain, not definite failure.
- [ ] Mock tests: approved edited payload exact, excluded pending/stale/protected, selected multi-recipe sibling only, create success/definite reject/timeout/5xx, repeated rerun zero new POSTs, crash after send before response/state write, double-submit.

### Task R3.2: History, tags and reconciliation

**Files:** `ui_data.py`, `ui_contracts.py`, `app_pages/import_history.py`, existing tag/StateStore owners; add `tests/test_ui_history.py`, extend `tests/test_mealie_tags.py`.

- [ ] History projection reads import ledger regardless of current cache. Render source/title/slug/time, creation and tag columns; filters and pagination. Show corrupted-history warning and block unknown protection rather than silently excluding records.
- [ ] Tag failure row says **Imported · tag needs attention**; Retry tag only wraps existing ensure/add behavior and never re-creates. Preserve pre-existing tags and original tag intent. Changed current batch tag never retags history.
- [ ] Check Mealie row has View/check guidance and disabled creation. Add a reconciliation command only after validating installed lookup/detail/slug evidence and additive audit contract. Confirmed-existing record links old identity to verified slug; confirmed-absent decision records evidence before releasing retry. Mere search miss is not sufficient absence proof.
- [ ] Separate scoped backfill/retag behind preview and explicit consent; use existing CLI behavior only where validated. Default history has no bulk retag.
- [ ] Test missing cache history, legacy records, tag exists/missing/fails, tag repair twice, preservation of other tags, uncertain found/not-proven-absent, permission revoked and sanitized errors.

**Accessibility:** table/list row actions have recipe names in accessible context; result counts announce full phrases without stealing focus; preflight editable without restarting whole workflow. **Browser:** ready selection/preflight → mock results; tag failure → repair only; timeout → Check Mealie. Screenshots `r3-preflight`, `r3-partial-results`, `r3-uncertain`, `r3-history-no-cache` and keyboard trace.

**Definition of done / handoff:** mock selected imports and crash protection verified; history matches per-recipe/tag ledger; exact live contract and reconciliation limitations documented. Live Mealie testing is **Unverified** until explicitly authorized and performed; do not block local mock handoff on network permission. Risks: no server idempotency, uncertain absence proof, create/write crash window. Excluded: Gemini, source re-extraction, attachment upload, editing existing Mealie content, ledger deletion instructions.

## R4 — Source intake, processing and recovery

### Objective and scope

Replace Upload placeholder with safe single/batch intake and resumable extraction. Dependencies: R1/R3 writer boundaries, R2 review, user-approved cloud/default choice. [Uploads wireframe](wireframes/ui-redesign/01-uploads-extraction.md).

### Task R4.1: Local batch and source intake

**Files:** `app_pages/upload.py`, `ui_data.py`; propose `ui_batch_store.py`; extend `tests/ui_fixture_factory.py`, add `tests/test_ui_batches.py` and `tests/test_streamlit_upload.py`.

**Proposed interface:** `stage_batch(name: str, tag: str, files: list[UploadedSource]) -> BatchManifest`. Manifest records batch/source IDs, digest, original basename, safe local path, accepted type/page count, status and related recipe IDs. Source hash is content identity; batch ID is grouping identity. Existing sources enter **Existing library** without moving or rewriting originals.

- [ ] Drag/drop + picker for JPG/JPEG/PNG/WEBP/PDF; reject unsafe paths and unsupported/invalid content, enforce documented configurable size/page limits before processing. New originals staged under dedicated batch storage with atomic durable write, not extraction cache files.
- [ ] Preserve duplicate filename inputs with collision-safe staged names; exact digest match offers **Open existing work** or explicitly add group reference, never silent re-extraction.
- [ ] Name batch and optional Mealie tag; show thumbnail/page count when known and per-file accepted/rejected result. Partial intake retains accepted files and avoids restaging them on rerun.
- [ ] Test all types, same filename/different bytes, exact-byte duplicate, partial invalid upload, refreshed session, unsupported/corrupt PDF and legacy library discovery. Display limits and storage failure recovery.

### Task R4.2: Extraction jobs and progress

**Files:** `app_pages/upload.py`, `import_recipes.py` extraction-only boundaries, `model_routing.py` usage/routing owner; propose `ui_job_service.py`; add `tests/test_ui_jobs.py`, reuse `tests/test_model_routing.py` and OCR tests.

**Proposed interface:** `start_extraction(batch_id: str, mode: str) -> JobSnapshot`, `pause_after_current(job_id: str) -> JobSnapshot`, `resume_extraction(job_id: str) -> JobSnapshot`. Snapshot distinguishes durable job/source outcomes, available recipe counts, known file totals and next action. Backend workers own progression; page poll/fragment only observes. Job state belongs to a small durable local manifest, not global mutable widget state.

- [ ] Support modes with honest network disclosure: Local OCR, Gemini extraction, OCR + assisted structuring. Reuse configured provider validation/routing; no model ID catalog assumptions. Display missing dependency/config before starting.
- [ ] Extraction service forces no Mealie calls, publishes per-source progress plus recipe-level results, records zero-recipe vs failed distinctly. Show candidates as they become durable, never approve automatically.
- [ ] Enforce one mutation owner for job/CLI and shared quota scheduling across workers; per-model counters remain best-effort and cannot see other applications. Restart identifies stopped work and resumes unresolved only, preserving previous attempts.
- [ ] Pause after current file stops future scheduling and states that in-flight work may finish. Explicit quota pause shows known reset time only if actually known; otherwise “Resume when quota is available”. No automatic endless retries or claim that model switch avoids account quotas.
- [ ] Poll progress using native fragments and immutable snapshots; rerun/second tab must not launch another worker. Bound lifetime and cleanup; don't tie worker ownership to browser session teardown.
- [ ] Tests: primary failure/fallback vs quality escalation, all quota-exhausted, bounded transient backoff with injected clock, OCR unavailable, local no network, assisted text route, source without candidate visible, restart/pause/rerun, concurrent tab start only one job, Review writes serialized with extraction publication.

**Accessibility:** drop alternative, file names/result text, progress status not color-only, errors near rejected input, no time-critical auto-disappearing message. **Browser:** first-run empty → synthetic files → preview → mock extraction → Review; quota pause/reload/resume recovery. Capture `r4-empty`, `r4-upload-partial`, `r4-extracting`, `r4-quota-paused`, `r4-source-failed`, `r4-completed`.

**Definition of done / handoff:** no CLI required for accepted modes in synthetic journey; sources/job progress survive restart, fake Mealie has zero calls, mock quotas bounded; user inspects intake wording and progress. Risks: page limits/storage, worker ownership, source discovery, provider config. Excluded: directory synchronization, cloud storage connector, automated cookbook page segmentation, new model-selection algorithm, live Gemini testing.

## R5 — Targeted retry and candidate reconciliation

### Objective and scope

Resume difficult sources without corrupting identity, drafts or prior imports. Dependency: source/job and exact-review contracts R1/R4, protected import R3. Add explicit comparisons rather than treating new extraction as new truth.

### Task R5.1: Unresolved retry plan

**Files:** `import_recipes.py` existing `select_sources`/reconciliation boundaries, `ui_job_service.py`, `ui_data.py`, `app_pages/upload.py`; extend `tests/test_retry_selection.py`, `tests/test_reconciliation.py`, `tests/test_ui_jobs.py`.

- [ ] Preview unresolved target source/candidate IDs, previous attempt, retained approved/imported siblings and network mode. Distinguish **Resume processing** from **Try a new extraction**.
- [ ] Intentional re-extraction creates a new version using existing hash/config cache rules and retains every prior cache/draft/decision. Never overwrite approved content to display a new attempt.
- [ ] Reuse existing safe matching; duplicate-title, rename/split/merge or uncertain mappings go to explicit compare queue. Human maps original↔new candidate, declares new recipe or defers. Protected identities cannot be declared new to bypass their state.
- [ ] If provider only accepts a whole source, disclose that the source is reprocessed; only unresolved candidate results may be promoted, never imported siblings. Do not promise candidate-only extraction requests unless supported.
- [ ] Test one imported/one unresolved sibling, reordered extraction, duplicate titles, split/renamed candidate, stale approvals and conflicting corrections. Zero new POSTs during extraction/reconciliation.

### Task R5.2: Comparison and preservation UI

**Files:** `app_pages/review.py`, `app_pages/upload.py`, `ui_review_state.py`; add `tests/test_streamlit_reconciliation.py`.

- [ ] Display old draft/current source/new extraction side by side in a focused comparison dialog or dedicated subview. Avoid a permanent third main pane. Show changed ingredient/step blocks and lineage; selection is explicit per conflicting section.
- [ ] Choosing new extraction creates a draft revision, preserves old correction and invalidates incompatible approval; no overwrite of source/history. Rebind issues only on certain target matches; lost targets remain unplaced.
- [ ] Protected sibling read-only summary visible. Cancel/leave returns to existing draft. Save mapping/readback before continuing.
- [ ] Tests: cancel no mutation, mapped content restorable, conflict save failure retains selection, duplicated title never auto-promoted and malformed extraction leaves prior valid version inspectable with warning.

**Accessibility:** differences have textual added/removed labels, not color-only; comparison controls keyboard operable; dialog returns focus to retry trigger. **Browser:** multi-page sibling retry with new reordered results and conflict defer; screenshots `r5-retry-plan`, `r5-identity-conflict`, `r5-extraction-comparison`.

**Definition of done:** targeted retry/reconciliation preserves identities and prior approval/import history; actionable held mappings; checkpoint reviewed before scale work. Risks: title-derived historical IDs, line/row mapping and differing source versions. Excluded: similarity auto-match, auto-ranking a “best” extraction, automatic replacement, auto-approval.

## R6 — Whole-product validation and scaling

### Objective and scope

Prove the coherent journey with realistic synthetic content and tune measured bottlenecks. Do not add features to compensate for failed UX. Dependency: approved prior checkpoints.

### Task R6.1: Large-library and state completeness

**Files:** `ui_data.py`, preview cache owners, queue/history pages, test fixture factory; add `tests/test_ui_large_catalog.py`; update `README.md` with primary UI instructions and retained CLI.

- [ ] Measure cold catalog load/warm candidate switch for 100/1,000/3,000 synthetic candidates and multi-page preview. Record machine/runtime, p50/p95, peak preview memory and full test command; target warm p95 ≤1s at 1,000.
- [ ] Cache projections by artifact revisions, invalidate after successful writes and external changes; do not cache stale approval/protection across dispatch. Bound preview cache entries/scales and queue pagination. Optimize only demonstrated hotspots; no speculative database.
- [ ] Verify source-only failures and ledger-only history remain visible, old/new attempt provenance identifiable, counts reconcile by source vs recipe, and first-run/end-of-queue copy never implies unfinished work is complete.
- [ ] Document UI launch, temporary fixture smoke procedure, save durability boundary, cloud modes, approval/import difference, uncertainty/tag recovery and CLI compatibility. No instruction to edit/delete ledgers to clear uncertainty.

### Task R6.2: Journey, accessibility and user checkpoint

- [ ] Playwright all six product scenarios with synthetic evidence: Fried Rice, Caramel cleanup, Cranberry uncertainty, PDF sibling partial completion, protected imported recipe and interrupted processing. Keep mocked service outputs explicit in screenshots.
- [ ] Run desktop/narrow/200% text plus keyboard-only and screen-reader-oriented DOM checks. Follow installed accessibility-audit input scopes; report actual criteria evidence and unresolved failures. No blanket WCAG compliance claim.
- [ ] Perform a user walkthrough after consent using the same sanitized tasks. Record observed actions/errors/task completion, not invented participant quotes. Compare against design targets; fix the most costly repeated friction before adding optional shortcuts.
- [ ] Check native theme polish across all empty/loading/error/partial/completed states, focus indicators and contrast pairs. Rereview actual screenshots after corrections.

**Automated acceptance:** full offline suite passes; no pending suggestion enters fake payload; resume/retry never re-creates protected work; exact revision/lineage tests remain green; compile/help/diff checks pass. **Browser evidence:** screenshot matrix and traces plus scope-limited accessibility findings and performance report. **Definition of done:** product checklist signed off with each Verified/Failed/Unverified/Blocked item, user accepts real editing ergonomics, no undisclosed live-service claim. Risks: performance hardware variation and browser widget limitations. Excluded: remote hosting/auth, telemetry service, mobile-first rewrite, keyboard shortcut framework, cover images/attachments.

## Minimal fixture-only smoke procedure

Existing factory is sufficient to boot the current app; R1 must extend its scans before judging visual correctness. Create isolated fixtures and set both paths before launching; do not run importer extraction modes for a UI smoke test.

```powershell
$fixtureRoot = Join-Path $env:TEMP ('recipe-ui-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $fixtureRoot | Out-Null
$env:RECIPE_UI_FIXTURE_ROOT = $fixtureRoot
& .\.venv\Scripts\python.exe -c "import os; from pathlib import Path; from tests.ui_fixture_factory import write_ui_fixture; write_ui_fixture(Path(os.environ['RECIPE_UI_FIXTURE_ROOT']))"
$env:RECIPE_IMPORTER_OUTPUT_DIR = Join-Path $fixtureRoot 'output'
$env:RECIPE_IMPORTER_SOURCE_DIR = Join-Path $fixtureRoot 'sources'
& .\.venv\Scripts\python.exe -m streamlit run streamlit_app.py --server.address 127.0.0.1 --server.port 8512 --server.headless true --browser.gatherUsageStats false
```

Use Ctrl+C to stop the server. Clear only those three task-specific environment variables afterward using `Remove-Item Env:RECIPE_UI_FIXTURE_ROOT,Env:RECIPE_IMPORTER_OUTPUT_DIR,Env:RECIPE_IMPORTER_SOURCE_DIR -ErrorAction SilentlyContinue`. New import/extraction milestones additionally inject fake clients/job services; path isolation alone is not sufficient once pages can call live services. Provide a development fixture-mode guard that refuses live client construction before those browser tests. Never rely merely on removing credentials.

## Milestone evidence and handoff template

Each checkpoint record belongs under `docs/wireframes/ui-redesign/evidence/` (directory created when real evidence exists). Include:

1. Baseline/final SHA and changed paths; no commit required.
2. Implemented tasks and deliberately excluded behavior.
3. Named failing→passing regression commands and actual counts; full suite, syntax, CLI help and diff status.
4. Fixture provenance and isolation/network guard; exact runtime and viewport.
5. Browser happy/recovery steps, screenshot/trace paths, observed results; never placeholder screenshots labeled real UI.
6. Accessibility findings/input scope and not-verifiable criteria; performance only when measured.
7. Safety audit: payload revision/proposal/history/identity behavior and create/tag outcomes, relevant limitations.
8. User decision at checkpoint and remaining Conditional/Blocking items with owner and next gate.

## Current handoff verification

**Verified:** current offline baseline 91 tests; source/read-model/AppTest findings in audit; local Streamlit version/signatures; documentation links/coverage and whitespace checked at completion. **Blocked:** local Playwright CLI not cached (`ENOTCACHED`). **Unverified:** rendered layout, keyboard/screen-reader/contrast behavior, proposed usability targets, deployed Mealie/Gemini integration, future milestone implementation. No new application code or regression tests were installed during this design assignment.

Luna should begin with **R1.1**, once this design is approved. Do not start by adjusting column widths, adding theme CSS or wiring Upload to `main()`.
