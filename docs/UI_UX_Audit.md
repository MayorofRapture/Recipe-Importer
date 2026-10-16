# Recipe-Importer UI/UX audit

Date: 2026-10-14. Baseline: `5df8558560ca7fd0bb9b2b24daf7573f4ad92e63`, branch `feature/m1-1-review-ui-redesign`.

This is an independent design assessment, not a UI release approval. Recommendations are proposals. No application changes, production artifact reads, or Gemini/Mealie calls were needed. `.env` was not opened. Historical batch counts are not current observations.

## Evidence and method

| Evidence | Scope and confidence |
| --- | --- |
| `streamlit_app.py`, all four `app_pages/` modules; `ui_data.py`, `ui_contracts.py`, `ui_review_state.py`, `draft_store.py`, `quick_edit.py` | Current composition and local state behavior; high confidence in code findings, no rendered-layout claims. |
| `import_recipes.py`, `recipe_quality.py`, `review_store.py`, `mealie_client.py`, `model_routing.py`, local OCR/structuring entry points | Ownership, payload, persistence, retry and review boundaries; code inspection, not live service verification. |
| `README.md`, `.env.example`, `requirements.txt`, root `AGENTS.md` | Documented operations and constraints. Current runtime reports Streamlit 1.66.0; minimum requirement remains broader. |
| `docs/UI_UX_Specification.md`, `docs/UI_Implementation_Roadmap.md`, `docs/wireframes/recipe-importer-primary-workflows.md`, M1.1 plan and verification | Prior intent and historical evidence; not proof of current usability. |
| `tests/`, `tests/ui_fixture_factory.py`, fixture README | Existing coverage and sanitized scenarios. The fixture PDF is a text placeholder, not representative scan evidence. |
| Fresh `python -m unittest discover -v` | **Verified: 91 tests passed**, zero failures, offline. Existing assertions miss important orchestration cases below. |
| Temporary AppTest/read-model probes | Three reproducible failures described below; no production output touched. These are exploratory checks, not newly installed regression tests. |
| Local Playwright capability check | **Blocked:** `npx.cmd --offline --package @playwright/cli playwright-cli --help` returned `ENOTCACHED`; Python Playwright also absent. No browser screenshots or focus/reflow measurements obtained. No dependencies installed. |

Installed skill guidance reviewed: Superpowers/brainstorming/planning/verification, Ponytail, recipe-review-ux and its checklist, product-design-and-ux including AI uncertainty/handoff, project Streamlit guidance, UI/UX Pro Max, accessibility-audit, Playwright, design/design-system, brand and companion skill entry points. Slide/banner and React/Tailwind advice is unsuitable for this desktop editing task. UI/UX Pro Max's local search recommended a marketplace hero pattern; that generic result was rejected because document correction needs workspace, not discovery marketing. Native Streamlit API signatures were inspected locally before recommending menus, wrapping containers and navigation.

## What works

- The backend already separates source extraction, diagnostics, proposals, human decisions and Mealie outcomes. Keep that foundation.
- Three native top destinations are shallow and understandable. Replacing navigation wholesale would add effort without fixing the actual review problems.
- Quick Edit preserves ingredient lines and instruction blocks; structured editing supports precise individual corrections in principle. Both are valuable.
- `DraftStore` appends durable records and performs flush/fsync; saves are separate from review events and source caches. `save_draft` reads back the record before reporting success.
- Confirmed imported and uncertain recipes are protected in the UI. CLI processing skips imported/uncertain identities and repairs eligible tags separately.
- Approve, save and import are distinct concepts. Pending suggestions and critical findings have an explicit visible approval gate.
- End-of-queue does not wrap to the beginning. Error paths attempt to keep the current editing buffer available.
- The test suite and local service boundaries make a gradual redesign feasible without live API writes.

## The five most important problems

### A01 — Approval does not identify the content that was approved

**Priority P0; verified projection defect, code-verified CLI divergence.** `ui_data.load_catalog` determines approval from the existence of the latest whole-candidate decision. It then overlays a draft without checking whether the decision belongs to that draft. In a temporary fixture, approve Fried Rice, save different ingredients afterward, reload: status is still **Ready to import**.

Separately, `_approve` writes a UI draft and records only whole-candidate `approved`. The CLI applies review events but never reads `ui_drafts.jsonl`. Thus the edited UI draft and recipe later constructed by the CLI can differ. This is a handoff integrity problem even though the UI currently cannot import.

**Root cause:** approval is modeled as a boolean permission, rather than consent to a particular recipe revision. **Change:** pin approval to an exact immutable content snapshot/revision and extraction lineage; share its projection with CLI import. Any later content edit invalidates eligibility. Preserve the current ledgers with additive compatibility events, not a rewrite. R1 must prove edited UI content reaches the existing payload boundary and later edits do not inherit consent.

### A02 — Durable drafts and proposal decisions compete for authority

**Priority P0; verified with a temporary fixture.** Save Cranberry Date Bars while its proposal is pending; accept the quantity through `ReviewStore`; reload `load_catalog`: ingredients remain `dates, cranberries` and the proposal is pending again. `load_catalog` applies review decisions, then replaces the whole result with a saved candidate. `buffer_for` also retains an existing session buffer after newer decisions. Accepting a proposal can therefore appear ineffective or drift from what will eventually import.

**Root cause:** a saved draft embeds review metadata and is allowed to replace newer decisions. **Change:** draft edits and decision authority need explicit precedence and revision checks. Replay decisions against their original extraction targets and merge only nonconflicting draft edits. If both changed the same field, show a conflict; do not select a winner silently. A critical diagnosis also needs an explicit resolution event; approving a proposal currently does not remove the UI's critical finding while the CLI filters approved fields separately.

### A03 — Structured editing is unreliable and expensive

**Priority P0 for correctness, P1 for efficiency.** Temporary AppTest: click the first **Move ingredient down** for Fried Rice. Expected `1 egg, 2 cups rice`; actual widget values and buffer remain `2 cups rice, 1 egg`. Keys use list indexes, and retained widget state writes old values into the reordered buffer on the next run. Delete/add and bulk-mode transitions carry related risks; those paths require regression tests rather than being assumed broken.

Every structured row also has a number, editable field and three wide action buttons in five columns. This multiplies controls and tab stops for a recipe with 20 ingredients. Long steps receive an 80px text area. Title, yield and description have no editable controls; timing/temperature exist only inside strings, not supported metadata fields.

**Root cause:** row position is doing the work of row identity, and row management is always visible. **Change:** stable UI row identifiers, a single explicit row menu, readable step editors, bulk-edit section mode, and supported metadata editing. Time/temperature remain source text unless an explicit backend schema extension is approved.

### A04 — The source and editor lose out to navigation chrome

**Priority P1; code-verified structure, usability hypothesis.** Before either pane the page renders a title/caption, bordered queue heading, four filter groups, navigation row, totals, recipe selector, divider, second title/status, action heading and five actions. Previous/next and recipe title are repeated. The Batch options are source hashes, so “batch” actually means a single source, not a human-named collection. `height=650` source/editor containers sit below this stack; no evidence shows they fit a 900px window.

All findings appear after the two panes. Non-indexed suggestions are omitted in structured mode; its renderer only displays recognized ingredient/instruction row indexes. Multiple suggestions for the same row collapse into one dictionary entry. Source page input permits 1–100 regardless of actual page count and offers neither zoom nor evidence targeting.

**Root cause:** independent forms were accumulated above a workspace instead of allocating the viewport around comparison. **Change:** one compact context line, one recipe header/action strip, two generous panes, queue drawer, actual page bounds, source zoom, field-local issues and a complete Issues view. The proposed wireframes replace the structure, not just widths.

### A05 — This is not yet an end-to-end product, and the catalog hides some work

**Priority P1; code verified.** Upload only links to Review. Import & history groups names with no actions, source identity, tag outcome or reconciliation. `load_catalog` builds rows from valid extraction caches; sources with no valid candidate and history records without a current cache are absent. A quota-exhausted file can therefore have no representation in Review. `TAG_FAILED` is normalized to Imported and its separate outcome is not surfaced by the history page.

**Root cause:** the interface treats successful extraction as the inventory instead of tracking sources, recipes and outcomes separately. **Change:** source-level batch activity and recipe-level review queues; independent history sourced from the ledger; import preflight and separate create/tag/reconcile actions. Do not imply that the existing CLI orchestration is already a usable UI job service.

## Additional findings

| ID / priority | Current evidence and consequence | Recommendation / proof needed |
| --- | --- | --- |
| A06 / P1 | `Skip`, queue buttons, filters and recipe selection do not invoke durable save or a dirty guard. Session buffers help some navigation, but refresh/restart loses unsaved text. A saved cue appears only after clicking Save. | Save on committed field changes plus guarded transitions. State exactly when text is durable; never promise every keystroke autosave with native widgets. Test refresh, failed-save and cross-page navigation. |
| A07 / P1 | `_approval_block_reason` checks critical findings/pending proposals but not the minimum recipe fields. Candidate approval readback checks only decision existence. | Shared backend validation and exact-revision readback; empty title/ingredients/instructions cannot pass. Failed/partial writes keep the same candidate selected. |
| A08 / P1 | Catalog identity is source digest plus normalized original title. Duplicate titles within one source can collide; renamed extraction candidates can appear new. UI direct identity lookup can return a record before CLI ambiguity checking. | Keep existing IDs for established records, freeze identity during title edits, and route split/rename/duplicate ambiguity to explicit reconciliation. No title-similarity auto-match. |
| A09 / P1 | A source preview image has alt text, but it says only “Recipe source page …”. It cannot make the scan readable to a screen reader. | Add separately labeled original OCR text when available, keyboard zoom/page controls and meaningful source context. OCR remains imperfect evidence. Requires rendered DOM/keyboard audit. |
| A10 / P1 | Malformed ledger lines can raise from store constructors. `_load_caches` silently skips invalid caches; users may see an incomplete inventory with no explanation. | Show “Some local records could not be read”, preserve files, identify artifact category and safe recovery. Corrupted latest cache fallback must disclose use of an older attempt. Never hide uncertain import history. |
| A11 / P2 | Every rerun parses caches and several ledgers, repeatedly scanning review decisions and state; source PDF rasterization repeats. | Cache previews by source hash/page/render scale; cache read-only projections by artifact revisions and invalidate after writes. Benchmark 1,000 recipes; avoid a rewrite or database until measurements justify one. |
| A12 / P2 | Alerts/warnings repeat; `field_path` and full source hashes appear in ordinary controls. | Human labels, local issue summaries and optional technical Details. Keep provenance discoverable. |

## Architectural constraints and design consequences

1. Streamlit reruns Python and input widgets commit on change/blur, not necessarily every keypress. Build an explicit committed-buffer save policy and flush focused inputs before navigation; verify in a browser. Widget state is not durable storage.
2. Native columns/containers can supply two panes, forms and menus. A fixed action bar, precise focus restoration and pan/zoom may need a small scoped component or CSS. Those are gated feasibility experiments; no frontend framework migration is justified yet.
3. Stores currently use locks per instance, not one process-wide/cross-process transaction. Multiple tabs and CLI/UI concurrent writers are not proven safe. R1 defines revision conflicts; job milestones require an explicit single-writer boundary.
4. `process_file` mixes extraction, review and creation. It cannot simply be invoked from Import for selected approved recipes: the UI needs a narrow existing-business-logic service adapter with no surprise extraction or sibling import.
5. The current model has `name`, ingredients, instructions, yield and description; no typed prep/cook time, oven temperature or page-to-field coordinate schema. Do not invent editable fields or highlights with no backing evidence.
6. Different files containing the same recipe are not covered by exact-byte duplicate prevention. Show “Previously seen source” only when the digest matches; a similar title can prompt manual review but cannot claim a duplicate has been proven.
7. Mealie behavior is code/documented expectation, not verified installed-release compatibility. No direct cookbook membership endpoint, server idempotency guarantee or dependable reconciliation lookup is assumed.

## Accessibility evidence boundary

No rendered markup or screenshot was available, so this is a Python source review, not a completed WCAG audit. Label text, image-alt argument and button labels exist; actual names/roles, contrast, focus order, traps, target dimensions, live announcements, zoom and reflow are **Unverified**. The “responsive” test checks literal container height/alt strings, not viewport behavior.

R2–R6 must retain a scoped accessibility-audit report against actual UI input. Test WCAG 2.2 SC 1.1.1, 1.3.1, 1.4.3, 1.4.4, 1.4.10, 1.4.11, 2.1.1, 2.1.2, 2.4.4, 2.4.6, 2.4.7, 2.4.11, 2.5.8, 3.2.2, 3.3.1, 3.3.2 and 4.1.2; authentication 3.3.8 is outside the initial local app. Add status announcement checks under 4.1.3. Screenshots alone cannot establish keyboard or programmatic relationships. No source-inspection inference is filed as a measured contrast or target-size failure.

## Recommendation and validation order

Address A01–A03 and approval completeness before changing appearance. Then inspect the two-pane review workspace with realistic synthetic scans, long instructions and 15 OCR artifacts. Stabilize this workflow before adding import, then upload/extraction. This sequence yields useful checkpoints for existing caches and avoids hiding integrity defects behind attractive screens.

Success targets in the product document are hypotheses for future usability checks, not measured improvements. No participants were observed; no browser session was completed. Existing tests passing is valuable safety evidence, not evidence that review is fast or pleasant.

## Related deliverables

- [Product experience](UI_Product_Experience.md)
- [Luna implementation roadmap](UI_Redesign_Roadmap.md)
- [Wireframe index](wireframes/ui-redesign/README.md)

The new documents propose replacing prior UX/layout decisions after approval. Prior specs, plans and historical verification remain intact as evidence; their commit instructions do not override the current no-commit constraint.

## Design-package completion checks

- **Verified:** 8 new documentation/wireframe files; 30 local links/HTML anchors resolve; UTF-8 content reads cleanly; static board has no remote assets or scripts.
- **Verified:** independent builder review returned no blocking questions after adding evidence-backed OCR-artifact removal and missing-source approval rules. These are proposed design decisions, not implemented behavior.
- **Verified:** targeted `python -m compileall -q streamlit_app.py app_pages ui_data.py ui_contracts.py ui_review_state.py draft_store.py review_store.py recipe_quality.py import_recipes.py tests` and `python import_recipes.py --help` both exited 0.
- **Verified:** `git diff --check`, plus `git -c core.autocrlf=false diff --no-index --check -- NUL <file>` for each new untracked document. Ordinary Git diff does not inspect untracked content; the extra check covers it without staging.
- **Verified:** tracked application files have no diff; no commit/push/merge performed. Git status reports pre-existing permission warnings for three temporary directories; those directories were neither accessed nor changed.
- **Blocked / Unverified:** Playwright capability check described above; no real rendered screenshots, accessibility conformance, usability measurements or live-service integration claimed. AppTest probes do not substitute for those checks.
