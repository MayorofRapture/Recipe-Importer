# Recipe-Importer current UI/UX assessment

Date: 2026-10-25. Checkout: `feature/complete-recipe-workspace`, baseline `7249d193e04232c6eab9ca9dc4662e649c5aa908` plus the UX-0 accessibility and harness correction commit. This is a verification baseline, not release sign-off.

## Verdict

**The three-workspace product is coherent in offline synthetic flows, while Review still spends too much viewport height above the editor and is too dense at 768px.** Keep the existing Upload -> Review -> Import & history workflow and its business rules. The Review layout redesign remains the next milestone; UX-0 corrected harness assumptions and restored focus after save/candidate navigation.

The Upload title-field locator resolves the visible `Recipe title` input. At 320px, the collapsed navigation exposes an accessible `Expand sidebar` button and Review is reachable by keyboard. Browser inspection found an application focus defect: after Save draft and candidate navigation, focus returned to the document body. UX-0 restores focus to the recipe title field. Full screen-reader conformance, 200% text resizing, live Gemini, and installed Mealie behavior remain unverified. Phase 3B.6 remains **Conditional Pass**.

## Evidence and limits

| Evidence | What it establishes | Limit |
| --- | --- | --- |
| `streamlit_app.py`, `app_pages/upload.py`, `app_pages/review.py`, `app_pages/import_history.py`, and current service/view-model boundaries | Current composition, visible controls, workflow ownership, and safety gates | Source inspection cannot establish rendered quality or accessibility-tree behavior |
| Synthetic Upload -> Review -> Import journey in cached Chromium, device scale factor 1 | Review renders at 1920x1080, 1440x900, 768x900, and 320x900; accessible names, narrow navigation, focus after save, and document overflow inspected | The capture hides only fixture attestation and local output-path captions. Source/editor usability at 320px remains poor; screen-reader behavior was not tested |
| `docs/wireframes/ui-redesign/evidence/browser-upload-partial-success-1440x900.png` and `browser-import-history-1440x900.png` | Upload partial acceptance and history after a synthetic fake-service journey | Earlier capture; synthetic data/services only, not live integration evidence |
| `docs/remediation/2026-10-24-remediation-roadmap.md`, Phase 3B.6 | Compatibility results and recorded gates | Results apply to their documented SHA; UX-0 does not upgrade live-service or Mealie compatibility status |
| GitHub PR #3 head after the roadmap-only documentation push | PR remained open and unmerged at baseline SHA `7249d193e04232c6eab9ca9dc4662e649c5aa908` | Current CI is reported separately for the exact pushed UX-0 SHA |

All recipes and uploaded files in screenshots are synthetic. No production `output/`, scan, `.env`, Gemini, or Mealie data was used. Capture hides the fixture attestation and local output-path captions. Normal app chrome still displays the selected local-library path; this remains a separate clutter/privacy finding for a later presentation fix.

## Workspace assessment

### Upload and cookbook range selection

The page supports named batches, optional batch tags, multiple local scan formats, per-file staging outcomes, extraction mode and consent, resumable job state, and cookbook PDF ranges. The first-time path starts with a batch form; the accepted types and independent file validation are visible. Partial success keeps valid files when another file is rejected. The staged files and job/range progress are understandable at desktop width.

The page is long and combines batch setup, all staged files, file preview, extraction controls, job progress, and cookbook-range tooling in one vertical flow. A user returning to an existing batch has both “Create a batch” and “Select an existing batch” before the current work. Keep this compact for repeat use: visually prioritize the active batch, current source outcomes, and one next action; keep range setup in a clearly separate section. Do not imply that approved cookbook range candidates can be imported: that gate remains intentionally disabled.

**First-time result:** understandable, though the upload-to-review continuity is mostly conveyed by status/copy and app navigation. **Repeat-use result:** batch switching and progress are available, but the user must scan a long page to find the relevant job or range.

### Recipe Review and source comparison

Review has real queue controls, skip/back, a skipped list, source page and view controls, selectable source text, recipe title/details, ingredient and instruction tabs, row actions, bulk edit, issue/proposal review, durable Save draft, and separate Approve & next. This is a meaningful review workspace rather than the older placeholder described in the October 14 audit. Keep the source authority, saved draft/review authority, and approval gates.

Rendered observations:

| Viewport | Observed result | UX consequence |
| --- | --- | --- |
| 1920x1080 | Source and recipe panes remain readable. Save draft and Approve & next fit in the initial viewport. | Wide desktop is the strongest comparison layout. |
| 1440x900 | Source and recipe panes begin near y=348. Save draft is at y=839; Approve & next begins around y=933 and is below the fold. | Approval needs a short scroll; the queue/navigation stack still uses substantial height. |
| 768x900 | Panes remain side by side at about 280-350 CSS px. Save/Approve are below the initial viewport; source and controls are cramped. No document horizontal overflow. | The layout technically fits but reduces reading and editing comfort. |
| 320x900 | The navigation is collapsed behind an accessible `Expand sidebar` button. Review is reachable by keyboard after expanding it. Source and recipe are not both comfortably usable in the initial viewport; the editor is a long scroll from source content. No document horizontal overflow. | Navigation works, but source comparison remains inefficient. Defer the layout correction to the next milestone. |

Review is optimized around a queue but not yet around sustained comparison. The controls above the panes compete with the recipe work. Issue and proposal context is lower in the page than the field it affects, so users can miss relevant evidence during a correction. Row-level Actions are repeated for every ingredient and step. The source can be read via source text, but the page selector/source view and editor are separate controls with no field-to-source anchor. Do not add spatial highlights unless OCR coordinates are available and validated.

**Save / Approve / Import:** these actions are separate and correctly labeled. Save draft is durable draft storage; Approve & next records review authority; Import belongs to the third workspace. Preserve that separation and make saved/approved status persistent in the compact recipe header.

**Unsaved work:** the synthetic browser journey saved a draft, reloaded the page, and verified the edited title was restored. AppTest coverage verifies that a dirty buffer remains selected when a navigation save conflicts and that approval is blocked while the buffer is unsaved. UX-0 did not simulate a browser-level storage conflict or interrupted process.

### Import and history

Import & history now has an approved-recipe selection, tag choice, preview/import preflight, and independent search/paginated history with outcomes and provenance. It explains that approval in Review is required before import. This repairs the old audit's claim that this destination was only a placeholder. Preserve the separate creation/tag/reconciliation outcomes and protected uncertain results.

The page combines a consequential import preflight with long-lived history. Make the visual boundary between these tasks clearer and keep history discoverable without suggesting that an old entry is eligible for a new POST. Range candidates remain excluded. No Mealie request was made in this assessment, so payload compatibility and live outcome handling are not visually or functionally verified here.

## Findings by category

### Confirmed application behavior correction

**Focus restoration after Save draft and candidate navigation.** Before UX-0, Chromium showed `document.body` as active after save and queue selection. The UI now restores focus to the recipe title field after these transitions. Real-browser assertions cover both paths; review/import authority is unchanged.

### Confirmed UX/accessibility defects

1. **P1 ? The app prints the full local library path in page chrome.** `streamlit_app.py` emits it as ordinary caption text rather than an optional diagnostic. Replace it with short library status and keep diagnostics opt-in.
2. **P1 ? Review does not protect the comparison canvas at 768px or 320px.** Side-by-side panes shrink at 768px. At 320px the collapsed menu is operable, but source and recipe are not both comfortably usable in the initial viewport. Switch to a labeled Source/Recipe view below a measured width; keep two panes on wide screens.
3. **P1 ? Approve & next falls below the fold at 1440?900.** Save draft is at y=839 and Approve & next begins around y=933. A compact action strip and shorter top stack would reduce the scroll needed to approve.
4. **P2 ? Repeated row menus increase tab stops and visual noise.** Retain keyboard reachability while reducing the repeated controls.
5. **P2 ? Issue/proposal context can be far from its editable field.** Keep a concise issue summary near the affected field and expandable evidence; never visually merge an unverified proposal into recipe text.

The priority labels describe user friction, not formal WCAG conformance. Screenshots do not prove accessible relationships, contrast in every state, keyboard operation, live announcements, or screen-reader behavior.

### Browser harness findings

- The current Upload journey finds `Recipe title` through its accessible label, edits it, and verifies the saved value after reload. The former timeout is stale; no application title-field defect was found.
- The old 320px driver attempted a hidden navigation anchor at x=-272. Chromium exposes a named `Expand sidebar` button. The harness now opens the navigation, targets only controls intersecting the viewport, and reaches Review by keyboard. The app navigation was operable; the negative-coordinate click was a harness defect.
- Save/Approve accessible names are verified in the Chromium accessibility tree. The focus ring is present on keyboard-focused Save draft. Focus is restored after a durable save and candidate change.

### Accessibility verification ledger

**Verified in the tested states:** no document horizontal overflow at 320px, 768px, 1440px, and 1920px; the accessibility tree exposes `Recipe title`, `Save draft`, and `Approve & next` with textbox/button roles; keyboard navigation opens the 320px menu and reaches Review; Tab reaches Save draft in eight steps from the Recipe title field; computed focus ring is visible; focus returns to the selected recipe title after save and queue navigation.

**Unverified:** full keyboard-only completion across every control; keyboard activation of Save/Approve (the local CDP driver traverses to these controls, but its Enter dispatch did not activate Save reliably); complete name/role/value inspection; error announcements; image alternative quality across source types; contrast across all states; 200% text resize; and screen-reader behavior.

Do not call the UI WCAG-conformant from this baseline. Before a release claim, complete the project accessibility review against current DOM, 200% text, error and focus states, and a full keyboard-only journey. Screen-reader validation was not performed.

## Five highest-impact improvements

| Rank | Change | User benefit | Current behavior affected | Complexity | Regression risk |
| --- | --- | --- | --- | --- | --- |
| 1 | Give Review a viewport-first desktop layout and source/editor switch at narrow widths | Faster correction with less scrolling; readable source at 768px | Pane stack begins below navigation controls; side-by-side remains at 768px | Medium | Medium: pane state, focus, tabs, and dirty edits across mode changes |
| 2 | Keep Save, Approve & next, and saved/approved state in a compact reachable header/action strip | Fewer trips to the bottom of long recipes; clearer interruption recovery | Actions render in the editor flow and can be below the 900px fold | Medium | High: preserve exact-revision approval and save semantics |
| 3 | Put issue/proposal context next to the affected field | Less context switching and lower chance of applying the wrong proposal | Issues/proposals are separated from some editable rows | Medium | High: suggestion must stay visibly unverified until explicit human action |
| 4 | Remove path/hash from the default page chrome | Less clutter and less local filesystem detail exposed | Header caption repeats full resolved library location | Small | Low: keep a discoverable diagnostics path for troubleshooting |
| 5 | Separate active import preflight from searchable historical outcomes | Easier to find new approved work without confusing it with protected past attempts | One long page contains both preflight and history | Small to medium | High: history must never imply authorization to retry a protected attempt |

## Sanitized screenshot set

Open the [annotated current Review captures](wireframes/ui-redesign/current-state-review-annotations.html). These PNGs use isolated synthetic data at device scale factor 1. The browser capture temporarily hides the synthetic fixture attestation and local output-path captions; no screenshot contains local filesystem metadata.

- [Review - 1920x1080](wireframes/ui-redesign/evidence/ux0-review-1920x1080.png)
- [Review - 1440x900](wireframes/ui-redesign/evidence/ux0-review-1440x900.png)
- [Review - 768x900](wireframes/ui-redesign/evidence/ux0-review-768x900.png)
- [Review - 320x900](wireframes/ui-redesign/evidence/ux0-review-320x900.png)
- [Upload - 1440x900 partial acceptance](wireframes/ui-redesign/evidence/browser-upload-partial-success-1440x900.png)
- [Import & history - 1440x900 synthetic history](wireframes/ui-redesign/evidence/browser-import-history-1440x900.png)

The screenshots show the current rendered app. The separate wireframe pages remain proposals and are not implementation artifacts.

## Release readiness gaps — separate from visual design

- **Live Gemini:** unverified. Provider credentials/model availability, latency, quotas, and real upload behavior were not exercised.
- **Installed Mealie compatibility:** unverified. No live create, tag, reconciliation, or installed-version contract check was performed.
- **Cookbook range import:** intentionally disabled. Do not enable it in the redesign; it requires separately authorized controlled rollout and compatibility evidence.
- **Browser/accessibility:** UX-0 baseline captured. The Upload locator and 320px Review navigation work in synthetic Chromium; full keyboard completion, 200% text, broad contrast/error-state coverage, and screen-reader behavior remain unverified.
- **Offline functional gate:** Phase 3B.6 Conditional Pass at `c22757d`; see [remediation roadmap](remediation/2026-10-24-remediation-roadmap.md). This design assessment does not upgrade that status.

## Scope boundary

UX-0 changed only the Review focus handoff and browser verification harness. It did not change review/import authority, candidate identities, extraction behavior, Mealie compatibility, histories, scans, or production output. `.vscode/` and `graphify-out/` remain untouched. No live service was contacted. The Review layout redesign has not started.
