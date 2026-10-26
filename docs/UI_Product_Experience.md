# Recipe-Importer product experience

Date: 2026-10-25. **Status: approved direction with conditional UX-0 verification complete. The Review layout redesign has not started.** Current observations are in the [UI/UX assessment](UI_UX_Audit.md); screen placements are in the [wireframe index](wireframes/ui-redesign/README.md). This specification describes presentation and interaction only. Existing Python services, source identity, review authority, and import protection remain authoritative.

## Product outcome

Help one Windows user turn batches of scanned recipes into a dependable Mealie collection with low repetitive effort and clear recovery after interruption. The highest-frequency task is correcting a recipe while comparing it with its source. The UI should make that source comparison easy to maintain without making inferred content look like transcription.

Keep the three existing destinations: **Upload**, **Review**, and **Import & history**. They represent different user decisions and map to current working flows. Do not introduce another dashboard or a second source of truth.

## Experience principles

1. Keep source evidence readable beside the recipe on wide screens; switch between them on narrow screens rather than compressing both.
2. Keep one obvious next action and put infrequent diagnostics behind disclosure.
3. Keep source transcription, authoritative recipe fields, unverified proposals, saved drafts, approval, and Mealie history visually distinct.
4. Saving is durable draft work; approving authorizes one exact revision; importing sends an approved revision. Never merge these actions or labels.
5. Preserve edits and focus when opening issues, switching source/editor, changing queue views, or returning after interruption.
6. Describe consequential states in plain language. A timeout means “Check Mealie,” not “Import failed” or “Retry now.”
7. Optimize keyboard travel and row corrections for hundreds of recipes, not only a short demo recipe.

## Information architecture

| Destination | Main question | Primary content | Primary action |
| --- | --- | --- | --- |
| Upload | What is in this batch and what should happen next? | Batch, sources, per-file outcomes, extraction/range job progress | Add sources or start/resume extraction |
| Review | What does the source say, and what needs a human decision? | Queue context, source page/text, editable recipe, local issues/proposals, save/approval state | Save draft or Approve & next |
| Import & history | Which approved revisions can be sent, and what happened before? | Import preflight, separate create/tag result, searchable history/reconciliation | Preview then explicitly import |

## Review interaction contract

### Wide layout

- At wide desktop widths, keep source and recipe visible together. Give the source sufficient width for an actual scan and preserve an independent scroll/zoom/page control without moving the recipe editor.
- Show a compact recipe identity line, queue position and plain-language status above the work area. Keep queue filtering and diagnostics out of the vertical path between that line and the panes.
- Keep Save draft and Approve & next visible with the title/status area where practical; if native Streamlit cannot provide a reliable sticky action area without losing focus or hiding content, retain a normal-flow action strip near the top and repeat the relevant action after the editor.
- Keep issue summaries and pending proposals near the field/step they affect. Expand rationale, source notes and detailed diagnostics on demand.

### Narrow layout and reflow

- Below the measured minimum width for a readable two-pane comparison, render one main pane at a time: **Source** and **Recipe** are explicit, labeled controls. Retain recipe title, current review state, queue access and Save/Approve actions in the visible context.
- The source view keeps page navigation and zoom controls next to the scan or selectable text. The recipe view opens at the affected field when entered from an issue/proposal link.
- At 320 CSS px, no horizontal scroll is required to reach content or actions. The top-level Upload/Review/Import navigation remains discoverable and operable by keyboard and pointer.
- At 200% text size, controls wrap and the user can scroll vertically without content overlap or focus obscuration.

### Editing and review states

- Preserve current Quick/Structured editing options only where each option solves a distinct task. Default ordinary review to directly readable ingredients and step text; provide section-level bulk edit for cleanup of OCR artifacts.
- Keep add/delete/reorder controls reachable and labeled. Avoid one menu per row when row operations can be grouped without losing keyboard access.
- Preserve source wording, order, quantity, unit, duration and temperature. A proposal stays a visibly separate, unverified card with its affected field, proposed value, rationale, uncertainty and pending/approved/edited/rejected disposition.
- Clearly show “Draft saved” with its revision and a dirty state when committed content changes. Navigation, page change, tab change, and queue transition must preserve or explicitly save edits; never discard silently.
- Approval confirmation names the candidate and exact current revision. Any edit after approval must visibly return the candidate to review before import.
- Do not suggest that all recipes are complete when source processing has unknown or failed work. Show per-source and per-recipe progress and the safe next action.

## Upload and cookbook intake contract

- Keep batch creation available for first use; for a returning user, make current/recent batches and their status the first visual group.
- Show accepted file types, filename, page count when known, and an independent saved/rejected result per selected file. A rejection must not erase accepted siblings.
- Separate ordinary source staging from the cookbook page-range workflow. Make the original PDF immutability and selected inclusive ranges understandable.
- Show extraction method, cloud consent when applicable, current job, per-source/per-range status, pause and resume actions. State in plain language that completed results are preserved.
- Keep synthetic/demo language out of real mode. Local paths and content hashes belong in optional diagnostics, not page chrome.
- Range candidates may be reviewed/approved but remain excluded from Mealie import in this phase.

## Import and history contract

- Import starts from an explicit selection of currently approved, complete, exact-revision candidates. Show selected count, source/batch tag intent and a preflight preview before any write.
- Present recipe creation and tag application as separate outcomes. Do not overwrite existing tags or silently backfill historical recipes.
- A definite rejection can be inspected and intentionally retried. A timeout or uncertain outcome is held under “Check Mealie” with reconciliation actions; it never gets a blind retry button as the default.
- History search and pagination remain separate from eligibility. A historic entry does not become eligible merely because it is visible or selected in history.
- Range exclusion and Mealie limitations are stated before a user expects import.

## Measurable acceptance goals (proposed hypotheses)

These are targets for a future user walkthrough; they are not observed performance claims.

| Scenario | Proposed target | What to record |
| --- | --- | --- |
| Find the next unresolved recipe in a batch of 100 | Reach the next recipe in 2 deliberate navigation actions after opening Review | Clicks/keystrokes, time, incorrect recipe selection, focus location |
| Correct one ingredient quantity and save | Source remains available while editing; save confirmation identifies revision; no unrelated fields change | Scroll changes, time, field values before/after, saved revision |
| Clear 15 OCR artifact lines | One section-level bulk-edit session and one Apply, with Cancel restoring prior text | Edit count, text preservation, cancel/apply result |
| Resume after closing the browser | Find the last saved recipe and continue without losing durable work | Re-entry actions, recovered candidate/revision, unsaved-state message |
| Reconcile an uncertain Mealie outcome | User can distinguish “Check Mealie” from definite failure and find the protected history entry | No duplicate POST, status comprehension, next action chosen |
| Work at 320px and 200% text | Navigate between workspaces and complete a review without horizontal scrolling or obscured focus | Reflow, keyboard path, names/roles/states, error identification |

## Out of scope

No changes to extraction quality/routing, recipe schema, identity, review decision semantics, durable ledgers, Mealie payloads/API assumptions, or import range gate. No remote or multi-user application model, no semantic auto-deduplication, and no custom frontend framework. Introduce a custom component only after a focused native Streamlit feasibility probe demonstrates a concrete blocker.
