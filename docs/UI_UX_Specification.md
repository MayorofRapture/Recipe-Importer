# Recipe-Importer UI/UX Specification v2

**Status:** Revised design proposal for review. No UI components, production writes, or live API calls are included.

**Primary outcome:** Open recipe → compare scan → correct content → approve and move to the next recipe.

**Default deployment assumption:** Single user, Windows, locally hosted Streamlit application on `localhost`. The existing Python importer remains independently executable.

## 1. Product intent

Recipe-Importer is a personal recipe workspace, not an extraction control panel. The operator should be able to process hundreds of recipes without understanding Gemini, OCR, cache filenames, API requests, or PowerShell.

The permanent interface has three destinations:

- **Upload** — add individual scans or cookbook batches and start/resume processing.
- **Review** — compare source pages with extracted recipes, make fast corrections, and approve safely.
- **Import & history** — send approved recipes to Mealie and reconcile recipe/tag outcomes.

Review is the first implementation milestone. Mealie import follows it. Future upload and extraction orchestration follows import.

### Goals

- Make Quick Edit the fastest path for noisy OCR and bulk corrections.
- Keep the source, authoritative recipe, unverified suggestions, drafts, approvals, and Mealie results visibly separate.
- Preserve work across mode switches, navigation, refresh, restart, retries, and partial failures.
- Make `Approve & Next` safe, predictable, and efficient for large queues.
- Explain suspicious OCR content without silently deleting or inventing information.

### Non-goals

- Automatic completion of facts absent from a source.
- A second extraction, review, or Mealie business-logic implementation inside Streamlit pages.
- A dashboard full of metrics or technical controls.
- Multi-user authentication for the initial local-only application.
- A direct cookbook-membership endpoint that Mealie does not expose; the batch tag is the grouping mechanism.

## 2. Evidence and preserved architecture

The existing importer, quality models, review store, model router, Mealie client, output artifacts, and prior v1 documents were inspected read-only. No source scans, `.env` secrets, or live service responses were opened.

| Existing contract | UX consequence |
|---|---|
| `RecipeCandidate` separates authoritative fields, diagnostics, suggestions, provenance, confidence, and `requires_review`. | The UI shows recipe data and review findings together but never merges an unverified suggestion into the authoritative recipe automatically. |
| Diagnostics distinguish missing, unreadable/cropped, ambiguous, and visible-but-not-extracted content. | Copy must distinguish “not printed in the source” from “the source contains something extraction did not capture.” |
| `Suggestion` records proposed value, rationale, confidence, uncertainty, diagnostic link, and review status. | Suggestion approval is separate from recipe approval. Pending suggestions are never importable. |
| `ReviewStore` is append-only and currently writes review CSV/events. | Drafts must use a new separate durable draft store; review decisions continue to use the existing ledger. |
| Extraction caches retain source hash, model, prompt/schema version, timestamp, and batch; OCR retains source text variants. | Drafts never overwrite caches. Details can show which extraction attempt is on screen. |
| Import state protects imported/uncertain candidates and records tag outcome separately. | Import history presents recipe creation and tag application as independent states. |
| Mealie create/tag operations are already implemented in Python. | Streamlit calls adapters around existing functionality; it does not duplicate payload or retry semantics. |
| Current output snapshot has 337 imported state rows, 38 review rows, and 39 review events. | Existing recipes and review work must be immediately usable; these counts are an observation, not a fixture or acceptance target. |

### User-facing vocabulary

| Internal concept | UI label | Meaning |
|---|---|---|
| `missing_from_source` | Not shown in the source | The recipe itself does not provide this information. |
| `unreadable` / `cropped` | Hard to read / Cut off | The source may contain it, but the evidence is not reliable. |
| `ambiguous` | Unclear in the source | Multiple readings remain possible. |
| `not_extracted` | Visible but not captured | Evidence appears present but was lost during extraction. |
| OCR suspicion | Check this text | A heuristic found content that may be corrupted or implausible. |
| Pending suggestion | Unverified suggestion | A separate proposal, never a source fact. |
| `NEEDS_REVIEW` | Needs attention | One or more issues block safe approval. |
| `UNCERTAIN` / `NEEDS_MANUAL_CHECK` | Check Mealie | Server outcome is unknown; reconcile before retrying. |
| `TAG_FAILED` | Recipe imported; tag needs attention | Recipe creation succeeded, tag operation did not. |

## 3. Information architecture

The top navigation contains only:

```text
Recipe Importer                  Upload     Review (4)     Import & history
                                 7 ready · 4 need attention · 1 check Mealie
```

Review is the default destination when unresolved work exists. The current batch, queue filters, and selected candidate persist through navigation and are encoded in safe query parameters where useful. A selected candidate is identified by its stable candidate identity, not its current array position.

The Review page is not a dashboard. The counts are navigation aids and next-action cues, not a reporting surface.

## 4. Review workspace

### Desktop layout

At a wide viewport, the working area has three regions:

1. **Queue rail:** compact recipe rows, search, batch/status filters, and progress counts.
2. **Source viewer:** PyMuPDF-rendered image/page preview, page navigation, recipe navigation, and source evidence.
3. **Recipe editor:** Quick Edit or Structured Edit, warnings, findings, suggestions, and actions.

The source stays visible while editing. The recipe, not the file, is the unit of work: a multi-recipe scan can show one imported recipe beside one unresolved sibling.

### Queue rail

Controls:

- Search title or source filename.
- Status: All, Needs attention, Ready to review, Ready to import, Imported, Check Mealie.
- Batch selector.
- `Show only unresolved` toggle.

Rows show title, short source identity, issue count, and state. Do not expose model IDs or full paths in the row.

```text
The Best Caramel Apples       Needs attention · 2 warnings
cookbook-page-12.pdf          OCR text needs checking

Fried Rice                    Ready to import
fried-rice.jpg                Approved
```

### Source viewer

- PyMuPDF-rendered page preview is the initial source-preview implementation.
- Supports JPG, PNG, WEBP, and multi-page PDF inputs.
- Page control: `Previous page`, `Page 2 of 4`, `Next page`.
- Recipe control is independent: `Recipe 2 of 3`.
- Evidence text identifies page/section/location where known.
- Preview loading has a text state: “Loading source page…” and a fallback if rendering fails.
- Original OCR/source text remains available through `View original text`; it is never replaced by cleaned text.
- Native PDF viewing may be added later only if browser testing demonstrates a better experience.

### Editor header

```text
The Best Caramel Apples                 Needs attention
2 warnings · Draft saved                Source page 2 of 4

[Quick Edit] [Structured Edit]
```

The mode switch is a segmented control or equivalent two-option control. The selected mode is not a recipe state and does not change identity.

## 5. Editing modes

### Quick Edit — default

Quick Edit is optimized for noisy OCR, paste, cleanup, and large corrections.

```text
Ingredients — one item per line
┌────────────────────────────────────────────────────────┐
│ 2 cups flour                                           │
│ 1 tsp salt                                             │
│ %                                                      │
│ Noy 0 9 2021                                           │
└────────────────────────────────────────────────────────┘

Instructions — blank lines separate cooking steps
┌────────────────────────────────────────────────────────┐
│ Step 1                                                  │
│ Mix ingredients.                                        │
│                                                         │
│ Step 2                                                  │
│ Bake until done.                                        │
└────────────────────────────────────────────────────────┘

Detected structure: 4 ingredients · 2 steps
Warnings: 2 items need checking
[Show structured preview]
```

Rules:

- Ingredients: one non-empty line becomes one ingredient entry. Preserve Unicode, fractions, punctuation, and wording exactly as typed.
- Instructions: blank lines delimit steps. Within each step, consecutive non-empty lines are joined with a single space, preserving the original text sequence.
- Leading/trailing whitespace around each line is trimmed for structure only; internal whitespace, Unicode fractions, symbols, and words are not normalized away.
- Empty ingredient lines are not entries. Empty instruction blocks are not steps.
- The preview shows the exact structured result before save.
- Suspicious text is highlighted and explained; it is not automatically deleted, normalized, or replaced.
- A parse warning may say “This line looks unusual; check it before saving.” It does not block Save draft by itself.
- Critical unresolved warnings block Approve & Next.

Quick Edit must make removing 15 OCR artifacts a few select-all/paste/delete operations, not 15 row operations.

### Structured Edit — optional

Structured Edit is for precise corrections:

- One labeled input per ingredient.
- One labeled input per instruction step.
- Add, delete, edit, and move up/down controls.
- Stable field paths and IDs, not visual index alone.
- A preview of the equivalent Quick Edit text.

This mode is intentionally more granular and is not the default for bulk cleanup.

### Exact mode-conversion behavior

The conversion contract is deterministic:

1. On every mode switch, first capture the current unsaved text/row values in local draft state.
2. Parse the current mode into a candidate structured representation without saving or changing review status.
3. Show a conversion preview with item/step count and warnings.
4. If parsing is lossless, switch immediately and populate the other mode.
5. If parsing would merge non-empty instruction lines, drop empty items, or create suspicious fragments, show the preview and require **Switch and keep preview** or **Stay in current mode**.
6. Never discard unsaved content. The raw unsaved text is retained in the separate draft store even if the operator stays in the current mode.
7. Switching modes never changes source identity, candidate identity, suggestion status, approval status, or import status.
8. Save draft writes the currently displayed authoritative recipe plus conversion metadata; it does not create a review decision.

### Structured preview

Both modes show a compact read-only preview:

```text
Result to save
Ingredients: 7 entries
Instructions: 5 steps
Warnings: 2 — review before approval
[View all warnings]
```

The preview is the operator’s check against accidental deletion or merging.

## 6. OCR quality warnings

OCR warnings are independent of model confidence. A high-confidence extraction can still contain suspicious content.

### Warning types

- Standalone `%`, isolated punctuation, or corrupted quantity symbols.
- OCR garbage such as `Noy 0 9 2021`.
- Instruction containing only `Step 1` or a label without an action.
- Unexpectedly fragmented instructions.
- Missing or implausible title.
- Mixed ingredient/instruction content.
- Unverified AI-generated completion.

Warnings appear beside the affected ingredient, instruction step, title, or proposal. Each warning has:

```text
Check this text
This line looks like OCR noise rather than a complete ingredient.
The original text is preserved. Correct it or leave it unresolved.
[Edit this line] [View source] [Leave for later]
```

The UI must distinguish:

- **Missing from source:** the original recipe does not state the value; a suggestion may be shown separately.
- **Lost/corrupted during extraction:** source evidence appears present or text is malformed; correct against the source.

No warning action silently invents, deletes, or auto-applies content. Critical unresolved warnings remain outside Ready to import.

## 7. Review actions and navigation

### Primary action: Approve & Next

When the current recipe passes validation and all critical issues are resolved:

1. Validate the current saved authoritative recipe.
2. Persist the latest draft if the approval form contains unsaved values.
3. Record the recipe approval separately from any suggestion decisions.
4. Confirm durable approval succeeded.
5. Select the next unresolved recipe using the current batch and queue filters.
6. Preserve the current filters and source/recipe context.

The screen must never advance after a failed save, failed validation, or failed approval record. The failure remains on the current recipe with the draft recoverable.

### Other actions

- **Save draft:** persist current authoritative edits and raw Quick Edit text; remain on the recipe.
- **Save & Next:** save successfully, do not approve, then move to the next unresolved recipe.
- **Skip for now:** persist no approval decision; optionally save a draft only when explicitly selected. Move to the next unresolved recipe.
- **Stay on current recipe:** close a success notice or continue inspecting; never changes status.
- **Approve suggestion:** records only the suggestion decision and final value. It does not approve the whole recipe.
- **Edit suggestion:** records the original proposal and the operator’s edited value separately.
- **Reject suggestion:** records rejection; the underlying critical finding remains until resolved.

### End of queue

After the final unresolved item:

```text
You have reached the end of this review queue.
4 recipes are ready to import · 0 need attention

[Open Import & history] [Review all recipes] [Stay here]
```

Do not wrap silently to the first recipe. The operator can intentionally reopen the full queue.

### Save failure and recovery

```text
We could not save this draft.
Your edits are still open in this session. Check the local draft store and try again.
[Retry save] [Download recovery text] [Stay on recipe]
```

The page remains on the current candidate. It never shows “Saved” until a readback confirms the durable draft record.

## 8. Durable draft storage

Drafts use a separate durable store, independent from the append-only review ledger.

### Draft record requirements

Each record includes:

- draft ID and stable candidate ID;
- source path/digest and batch ID;
- authoritative recipe fields at the time of save;
- raw Quick Edit ingredient/instruction text;
- current editing mode;
- structured preview/parse warnings;
- source extraction version and timestamp;
- draft revision, saved timestamp, and last writer;
- optional link to review decision after approval.

Draft writes are atomic and read-back verified. Existing extraction caches and review events are never overwritten. On startup, the app restores the latest durable draft for each candidate and shows `Draft restored`.

Approval always reads the latest saved, validated authoritative recipe. A browser-only value that was not durably saved cannot be approved.

## 9. Import & history

The import page contains:

1. **Ready to import:** approved authoritative recipes only.
2. **Preflight:** count, batch tag, excluded pending/unresolved recipes, and duplicate-protection summary.
3. **Results:** recipe creation and tag application shown separately.
4. **Reconciliation:** definite failure versus Check Mealie.

```text
Ready to import: 3
Excluded: 2 need attention · 1 pending suggestion
Batch tag: October 2026

[Import approved recipes]
```

Result labels:

```text
Recipe: Imported          Tag: Applied
Recipe: Imported          Tag: Needs attention     [Repair tag]
Recipe: Import failed     Tag: Not attempted       [Review error]
Recipe: Check Mealie      Tag: Not attempted       [Reconcile]
```

Check Mealie disables blind retry until the operator confirms reconciliation. Tag repair never recreates an already imported recipe.

## 10. Upload and future batch workflow

Upload remains simple even though it is implemented after import:

1. Choose files or drag-and-drop JPG, PNG, WEBP, or PDF.
2. Show filename, preview/thumbnail, page count, duplicate indication, and file validation.
3. Name/select cookbook grouping and optional batch tag before processing.
4. Start or resume a durable job.
5. Show file and recipe progress, including mixed outcomes inside multi-recipe files.
6. Link unresolved results to Review and approved results to Import & history.

Long-running extraction must not depend on an open Streamlit browser session. M3 defines a recoverable worker/job boundary and persistent state outside `st.session_state`; the current importer does not automatically gain background guarantees from this design.

## 11. Visual system

Direction: quiet cookbook utility — warm paper surfaces, high-contrast ink, restrained navy actions, calm spacing, and no dashboard ornament.

| Token | Value | Use |
|---|---|---|
| Ink | `#1A1A1A` | Body/headings |
| Paper | `#FDFBF7` | App background |
| Surface | `#FFFFFF` | Queue/editor cards |
| Border | `#D8D4CA` | Grouping |
| Trust navy | `#1E3A5F` | Primary actions/focus |
| Success | `#176B3A` | Approved/imported, with text |
| Attention | `#8A5A00` | Warnings, with text |
| Error | `#A12A2A` | Definite failures only |

Use sentence case, readable 16 px body text, a 4 px spacing scale, no emoji icons, and Material Symbols where an icon helps. No state is communicated by color alone. Keep motion optional and honor reduced-motion preferences.

## 12. Accessibility and responsive behavior

- All fields, page controls, warning actions, mode switches, and queue rows are keyboard reachable with visible focus.
- The queue → source → editor → warnings → action-bar focus order is explicit.
- Failed validation moves focus to a summary linked to inline field errors; toast-only feedback is not sufficient.
- Text contrast is at least 4.5:1 for normal text, 3:1 for large text and non-text indicators.
- Controls are at least 24×24 CSS pixels; primary touch targets should be 44×44 where practical.
- Image/PDF previews have accessible names and adjacent filename/page/evidence text.
- No drag/drop-only action exists; every upload has a file-picker path.
- At 320 px and 200% text zoom, the layout has no horizontal scrolling for the task content.
- Wide Review: queue/source/editor columns. Narrow Review: queue above recipe, source as “View source” disclosure, actions in normal flow.
- Native PyMuPDF preview errors remain understandable without visual inspection.

Static screenshots can assess hierarchy, labels, contrast, and spacing. Keyboard operation, focus movement, accessible names, and DOM relationships require AppTest/Playwright or browser accessibility evidence after implementation.

## 13. Streamlit constraints and alternatives

| Constraint | Design response |
|---|---|
| Reruns happen after widget interaction. | Use forms for each edit mode, stable widget keys, and durable drafts outside session state. |
| Session state is not durable. | Use it only for selection/filter/temporary form values; show “Saved” only after durable readback. |
| Native columns are not independently scrollable application panes. | Use responsive containers and a compact queue; source becomes a disclosure on narrow screens. |
| Ordered list editing is awkward in tables. | Quick Edit uses native multiline text areas; Structured Edit uses labeled inputs and explicit move controls. |
| PyMuPDF preview is not a native Streamlit viewer. | Render selected PDF pages to bounded preview images on the Python side, cache them, and expose text context. No frontend framework is needed. |
| Long extraction jobs can outlive a browser rerun. | M3 uses a persistent worker/job adapter; Streamlit reads state and does not own the job lifecycle. |
| Browser-only focus, layout, and file interactions are not fully covered by AppTest. | Use AppTest for state/widget behavior and Playwright for rendered browser, responsive, focus, and preview checks. |
| Many queue rows can be expensive. | Render summaries only, paginate or window the queue, and load the selected source page lazily. |

Use native Streamlit controls first. Do not add React, a client-side database, or a custom drag/drop component for M0/M1.

## 14. Representative scenarios

### Fried Rice

After selecting the recipe, the operator edits a field in Quick Edit, sees the structured preview, saves, and uses Approve & Next. The flow is no more than six deliberate actions after selection. A failed durable save leaves the operator on the recipe.

### The Best Caramel Apples

The extracted recipe contains 15 OCR garbage ingredient entries, including standalone symbols and date-like noise. Quick Edit lets the operator remove or replace the block with a few multiline edits. Suspicious content is marked beside the affected lines; original OCR remains viewable. The operator sees the resulting ingredient count and preview before saving. No line is silently discarded.

### Cranberry Date Bars

Twelve fragmented instruction entries are pasted/combined in Quick Edit into five blank-line-separated steps. The preview confirms five structured steps. Ambiguous or unreadable source details remain warnings and block approval until corrected or explicitly resolved by the documented policy.

### Multi-page PDF

Page navigation and recipe navigation remain independent. Recipe 1 can be approved/imported while recipe 2 needs attention. Retry and queue selection use stable identities; successful recipe 1 is protected from duplicate creation.

## 15. Measurable acceptance criteria

These are design-time criteria for future implementation. They are not claimed as executed in this revision.

| ID | Acceptance measure | Evidence required |
|---|---|---|
| UX-01 | Fried Rice can be reviewed, corrected, saved, and approved in six or fewer deliberate actions after selection. | AppTest plus human task trace. |
| UX-02 | The Best Caramel Apples removes 15 OCR garbage entries using Quick Edit in at most three content-edit submissions, without losing Unicode/fractions or silently dropping text. | Fixture assertion, before/after preview, browser trace. |
| UX-03 | Twelve fragmented instruction entries become five steps through multiline editing, not twelve individual row edits. | Fixture parse test and Playwright trace. |
| UX-04 | Approve & Next advances only after durable approval readback and preserves batch/filter state. | AppTest state transition test. |
| UX-05 | A failed save/approval leaves the current recipe selected and the draft recoverable. | Failure-injection test and browser trace. |
| UX-06 | End-of-queue state offers Import & history without wrapping to the first item. | AppTest and screenshot. |
| UX-07 | Switching Quick/Structured modes preserves unsaved work, identity, and review status. | Round-trip conversion tests, including warning cases. |
| DATA-01 | Drafts survive refresh, navigation, and application restart without altering extraction caches or review history. | Durable-store restart test. |
| DATA-02 | Critical unresolved warnings and pending suggestions are absent from Ready to import and Mealie payloads. | Deterministic payload/selection test. |
| DATA-03 | Existing imported recipes and Check Mealie items cannot be recreated by retry. | Fake Mealie call-count test. |
| DATA-04 | Recipe and tag results remain separate; tag repair never creates a recipe. | Import-boundary test. |
| OCR-01 | Each listed suspicious-content class appears beside the affected field with plain-language correction guidance. | Synthetic warning fixture and screenshot. |
| A11Y-01 | Review controls, mode switching, multiline editing, warning actions, and Approve & Next work keyboard-only with visible focus and no trap. | Playwright keyboard trace. |
| A11Y-02 | Validation focus moves to an error summary with linked inline errors. | Playwright focus assertion. |
| A11Y-03 | Review remains usable at 320 px and 200% text size without task-content horizontal scrolling. | Browser screenshots and DOM check. |
| PERF-01 | Warm cached recipe open is ≤500 ms; draft save confirmation is ≤1 s on the target local machine. | Timed local test with machine details. |
| PERF-02 | A 1,000-row queue renders summaries without loading 1,000 full editors/previews. | Fixture load test. |
| VERIFY-01 | Representative workflows are tested with AppTest and Playwright when implemented. | Recorded test output/traces. |

## 16. Resolved defaults and remaining decisions

### Adopted defaults

- Windows, single user, local `localhost` Streamlit deployment.
- Separate durable draft storage; existing append-only review ledger preserved.
- PyMuPDF-rendered previews initially; native PDF viewing later only if validated.
- Existing Python importer remains the import/extraction authority.
- Long-running work gets a persistent job/worker boundary in M3, not a browser-session promise.

### Still open

1. Exact draft file/schema location and backup policy.
2. Maximum source file size/page count for PyMuPDF rendering.
3. Whether “explicitly resolved” critical findings means a new review disposition or only a corrected authoritative value; this must be owned by the data-contract implementation.
4. Exact live Mealie version and reconciliation lookup contract; no live call is authorized in the design phase.
5. Whether batch tags are optional or required for each import.

