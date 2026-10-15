# Recipe-Importer UI Implementation Roadmap v2

**Status:** Proposed plan only. Do not implement until the revised design is approved.

**Sequence:** M0 contracts/fixtures → M1 Review → M2 Mealie import/history → M3 Upload/extraction → M4 polish/scalability.

The sequence deliberately eliminates PowerShell from reviewing and importing existing recipes before adding future cookbook-upload orchestration. Streamlit pages are presentation/orchestration adapters; existing Python modules remain the owners of extraction, review decisions, import payloads, duplicate protection, tags, and retry semantics.

## Cross-cutting architecture rules

- Use a thin Streamlit adapter over existing importer modules. Do not duplicate extraction, review, or Mealie business logic inside pages.
- Keep source/evidence, authoritative recipe data, unverified suggestions, durable drafts, review decisions, and import history as separate concepts.
- Use stable candidate identities and source digests; never use the current `recipes[]` position as the sole identity.
- Keep the append-only review ledger unchanged. Add a separate durable draft store.
- Never overwrite extraction caches or production output as part of UI development.
- Use synthetic fixtures/fake services by default. Live Gemini/Mealie verification requires explicit approval.
- Report `Verified`, `Failed`, `Unverified`, and `Blocked` separately.

## M0 — Data contracts and fixtures

### Objective

Create the safe boundary the UI can read and write without touching production services.

### Deliverables

- Read-only adapters for extraction caches, OCR/source text, review events/CSV, import state, and run reports.
- Stable view models for batches, sources, candidates, warnings, suggestions, drafts, review decisions, recipe results, and tag results.
- Stable identity/reconciliation adapter that survives recipe reordering and multi-recipe files.
- Separate durable draft store with atomic writes and read-back verification.
- Deterministic parser/serializer for Quick Edit and Structured Edit.
- Synthetic fixtures:
  - Fried Rice, one readable image and one small correction.
  - The Best Caramel Apples with 15 OCR garbage ingredient entries.
  - Cranberry Date Bars with 12 fragmented instructions becoming 5 steps.
  - Multi-page PDF with multiple recipes and mixed outcomes.
  - Pending/approved/edited/rejected suggestions.
  - Imported, tag failure, definite import failure, and Check Mealie records.
- Redaction checks ensuring secrets and private source content do not appear in fixture logs.

### Required modules/components

- `ui_adapters` or equivalent read/write boundary.
- `draft_store` with revision and recovery semantics.
- `quick_edit_codec` for line/block parsing and structured preview.
- `ui_view_models` for user-facing statuses and copy.
- Fixture factory and fake Mealie boundary.

Names are provisional; locate them beside existing business modules rather than creating duplicate logic.

### Integration and data safety

- Existing extraction caches are read-only inputs.
- Existing review events remain append-only and are only extended through the existing review API.
- Drafts contain the current authoritative edit and raw Quick Edit text, but do not rewrite source or cache.
- M0 has no production writes and no Gemini/Mealie calls.

### Acceptance tests

- Cache/review/import adapters load legacy-shaped fixtures without migration.
- Candidate identity remains stable after array reordering.
- Pending suggestions do not appear in an import-ready view model.
- Draft write/readback survives process restart and detects revision conflicts.
- Quick Edit round trips Unicode, fractions, punctuation, and blank-line instruction steps.
- Suspicious lines remain present in the parsed result and produce warnings.
- Recipe/tag outcomes remain separate.

### Manual verification

- Inspect fixture payloads to confirm original suggestion/source text remains visible.
- Review the user-facing labels with no model IDs, API terms, or raw status codes in the primary view models.

### Dependencies and blockers

- Exact draft file/schema location and backup policy.
- Confirmation of the rule for explicitly resolving critical findings.
- No UI code should begin until the adapter and fixture contracts are approved.

## M1 — Review workspace

### Objective

Deliver the permanent Review workflow:

```text
Open recipe → compare scan → Quick Edit correction → save → Approve & Next
```

### Deliverables

- Three-destination navigation shell: Upload, Review, Import & history.
- Review queue with search, batch/status filters, unresolved-only view, stable selection, and queue counts.
- Side-by-side source viewer using PyMuPDF-rendered page previews; independent page and recipe navigation.
- Quick Edit as the default mode:
  - multiline ingredients, one item per line;
  - multiline instructions, blank lines separate steps;
  - structured preview and suspicious-content warnings;
  - paste/delete/replace optimized for bulk cleanup.
- Structured Edit as the optional precise mode:
  - per-entry edit/add/delete/move;
  - stable field paths;
  - equivalent Quick Edit preview.
- Exact mode conversion with unsaved-work preservation and conversion warnings.
- OCR warning cards beside affected fields, including immediate correction links and original-text access.
- Separate unverified suggestion cards and suggestion decisions.
- Durable draft save/restore with visible `Saved`, `Unsaved changes`, `Saving`, `Draft restored`, and failure states.
- Review actions:
  - Approve & Next;
  - Save draft;
  - Save & Next;
  - Skip for now;
  - Stay on current recipe.
- End-of-queue experience that links to Import & history without wrapping.
- AppTest and Playwright verification plan.

### Required modules/components

- Streamlit entrypoint using `st.navigation` and `st.Page`.
- Review queue page and view-model adapter.
- PyMuPDF preview renderer/cache.
- Quick Edit codec and warning renderer.
- Structured Edit form.
- Durable draft adapter.
- Approval transition adapter that validates/read-backs before advancing.

Pages must not call Gemini or build Mealie payloads.

### Integration with existing importer functionality

- Read `RecipeCandidate`, diagnostics, suggestions, OCR text, cache metadata, review state, and import history through adapters.
- Record suggestion/candidate decisions through existing review semantics.
- Reuse existing identity and quality rules rather than reimplementing them in Streamlit.
- Import remains unavailable from M1 except for a disabled/link-only “Ready to import” destination.

### Data-safety requirements

- Never advance after a failed draft write or approval write.
- Approval reads the latest durable draft, not an unsaved browser value.
- Skip is never approval.
- Pending suggestions and critical unresolved warnings remain outside Ready to import.
- Existing Imported and Check Mealie candidates are protected.
- Original OCR/source text and old extraction caches remain recoverable.

### Acceptance tests

- Fried Rice: six or fewer deliberate actions after selection to correct, save, and approve.
- The Best Caramel Apples: remove 15 garbage lines in at most three multiline edit submissions; no silent deletion.
- Cranberry Date Bars: combine 12 instruction fragments into 5 steps through Quick Edit.
- Mode switch preserves unsaved text and candidate identity in both directions, including suspicious fragments.
- Approve & Next advances only after durable approval readback and preserves queue filters.
- Save & Next advances without approval; Skip advances without approval.
- Failed save/approval retains current recipe and recoverable draft.
- End of queue does not wrap and provides Import & history action.
- Pending proposal cannot become import-ready.

### Manual usability and accessibility verification

- Walk the four representative scenarios using only the UI; no PowerShell.
- Playwright keyboard trace covers queue selection, mode switch, multiline editing, warning correction, Save draft, and Approve & Next.
- Playwright screenshots cover empty, review, warning, saved, failed-save, and end-of-queue states.
- Check 320 px reflow, 200% text, visible focus, error summary, and preview loading fallback.
- Run the accessibility audit with its input scope stated; screenshot evidence is not presented as DOM/keyboard proof.

### Dependencies and blockers

- M0 adapters, draft store, Quick Edit codec, and fixtures.
- PyMuPDF availability and accepted preview size/page limits.
- Draft conflict policy if more than one browser tab is opened accidentally.

## M2 — Mealie import and history

### Objective

Connect approved recipes to Mealie through existing Python functionality before adding new uploads.

### Deliverables

- Import & history page.
- Preflight count of approved recipes, excluded unresolved/pending items, selected batch tag, and duplicate-protection summary.
- Thin adapter to the existing recipe-create operation.
- Separate tag ensure/apply/repair adapter.
- Recipe result and tag result displayed independently.
- History and reconciliation for Imported, tag failure, definite failure, and Check Mealie.
- Intentional single/batch import action with durable per-recipe outcomes.

### Required modules/components

- Import orchestration adapter wrapping existing Python functions.
- History/state projection.
- Fake Mealie client for tests.
- Reconciliation view model and confirmation action.

### Integration with existing importer functionality

- Reuse `schema_recipe`, `MealieClient`/existing import path, `StateStore`, and tag logic.
- Do not duplicate request payload construction or error classification.
- Use existing state identity and status protection.

### Data-safety requirements

- Pending suggestions and unresolved critical warnings are excluded at the adapter boundary.
- Re-running Imported candidates creates no second recipe.
- Uncertain responses become Check Mealie and disable blind retry.
- Tag repair never creates a recipe.
- Existing tags are preserved.

### Acceptance tests

- Create success, definite failure, uncertain response.
- Tag exists, tag creation, tag already present, tag failure after recipe creation, tag repair.
- Multi-recipe partial success with no duplicate retry.
- Preflight payload contains only approved authoritative recipe fields.

### Manual verification

- Fake-service happy path and recovery path in the browser.
- Explicit review of copy: “Check Mealie” never says “failed” when the outcome is uncertain.

### Dependencies and blockers

- M1 approved recipe/draft contract.
- Exact installed Mealie version and live reconciliation lookup contract remain to be verified separately with approval.

## M3 — Upload and extraction

### Objective

Add individual and cookbook-batch uploads, extraction progress, OCR/Gemini orchestration, retries, and resumability.

### Deliverables

- Upload page for JPG, PNG, WEBP, and PDF.
- Cookbook grouping and optional batch tag before import.
- File/page previews, duplicate indication, validation, and batch summary.
- Persistent worker/job record outside Streamlit session state.
- Human-readable extraction progress by file and recipe.
- Retry unresolved files/recipes only.
- Cached extraction reuse and intentional re-extraction controls.
- OCR fallback, model routing, quota/backoff, and diagnostics surfaced progressively.

### Required modules/components

- Upload adapter and batch/job store.
- Worker entrypoint or scheduler boundary independent of the browser.
- Progress projection.
- Existing model router/OCR/importer adapters.

### Integration with existing importer functionality

- Existing Python importer remains the execution authority.
- Streamlit submits/resumes jobs and reads durable state; it does not run long extraction inline in a page rerun.

### Data-safety requirements

- Preserve source files, cache versions, approvals, drafts, and imported records.
- Do not silently reuse stale cache after intentional configuration change.
- Multi-recipe identity must remain stable across re-extraction/reordering.
- Local usage counters are presented as best-effort; they do not claim awareness of external provider traffic.

### Acceptance tests

- One file/one recipe and one file/multiple recipes.
- Multi-recipe partial retry.
- Upload/restart/browser-close/resume without duplicate work.
- Primary failure, quality escalation, quota fallback, all quota-exhausted, transient retry/backoff.
- OCR fallback and suspicious-content warnings.

### Manual verification

- Browser progress/recovery workflow with a fake worker.
- Large PDF preview/loading behavior and source-page navigation.

### Dependencies and blockers

- M2 import history and M1 source/draft identity.
- Worker/job design must be approved before claiming background execution.

## M4 — Polish and scalability

### Objective

Make the permanent interface pleasant for large batches without broadening its scope.

### Deliverables

- Queue summary pagination/windowing and lazy source rendering.
- Performance improvements measured against 100/1,000-recipe fixtures.
- Carefully bounded bulk operations that reuse individual validation/safety gates.
- Advanced accessibility verification and remediation.
- Quality-of-life improvements discovered through task observation.

### Acceptance tests

- 1,000 summary rows remain navigable without rendering 1,000 full editors.
- Warm cached recipe opens in ≤500 ms on the target local machine.
- Draft save confirmation in ≤1 second on the target local machine.
- Bulk actions cannot bypass pending/critical/duplicate protections.

## Verification policy and handoff

For each milestone, retain:

- automated test output with counts;
- AppTest evidence for Streamlit state/widget behavior;
- Playwright traces/screenshots for browser-only behavior;
- accessibility findings with scope;
- data-safety/duplicate-protection evidence;
- limitations and unverified live behavior.

Recommended handoff:

1. Approve this v2 specification and wireframes.
2. Implement M0 contracts and fixtures only.
3. Review M0 test evidence and approve M1.
4. Implement M1 Review without Mealie writes.
5. Run AppTest, Playwright, accessibility, and representative usability checks.
6. Only after M1 approval, implement M2 import/history.

Stop after design approval; do not implement M0 or M1 in this revision.

