# D3 — Upload and Import/History implementation report

**Status: implemented; independent review requested.** D3 changes are limited to the existing Upload and Import & history workspaces. The three-workspace architecture, service eligibility, import plan identity, confirmation gate, and cookbook range exclusion remain in force.

## Upload

Returning users now see their selected batch and status, selector/recent context, active extraction job and controls, file outcomes, then upload and extraction setup. New batch creation is available in a collapsed section after extraction setup. When no batch exists, **Create your first batch** is presented as the initial action. The cookbook PDF/range workflow remains last and range candidates remain excluded from Mealie import.

Cloud consent is scoped to the selected batch, source identities/digests, extraction mode, and non-secret extraction settings. Changing that scope clears consent. Staging newly selected sources now reruns the page once so the file list reflects the staged result immediately; previously staged sources are not staged again. Selector labels no longer show stale file counts; the selected batch status carries the current count.

## Import & history

The Ready to import workflow continues to use the existing preview and dispatch services, exact saved revisions, selection/tag plan signature, preflight validation, and explicit final confirmation. Recipe creation and tag application outcomes are shown separately. Cookbook range exclusion remains visible before the import action.

History and recovery controls now live under a collapsed **History and recovery** section. History outcomes distinguish successful imports, tag attention, definite rejection, uncertain creation requiring a Mealie check, and protected/unreconciled records. Existing reconciliation evidence, acknowledgments, attempt identity, tag repair, search, and pagination remain available. History display does not create eligibility, and uncertain creation does not offer repeat creation.

## Browser evidence

Evidence is in [the D3 evidence folder](evidence/d3/). Screenshots and JSON reports are sanitized synthetic-data artifacts; the original UX-0 evidence remains unchanged for before comparison: [Upload empty](evidence/upload-empty-1440x900.png), [Upload with a batch](evidence/upload-created-1440x900.png), [previous partial upload state](evidence/upload-partial-success-1440x900.png), [import preview](evidence/browser-import-preview-1440x900.png), and [import history](evidence/browser-import-history-1440x900.png).

| Viewport | Upload hierarchy / overflow | Review save and approval | Import & history |
| --- | --- | --- | --- |
| 1440×900 | Required order verified; document width 1440; no horizontal overflow | Both actions visible without scrolling; focus restoration and visible focus recorded | Ready to import, then expandable searchable History; creation and tag results separate |
| 320×900 | Required order verified; document width 320; no horizontal overflow | Both actions visible; explicit Recipe pane selected for the journey | Controls wrap within viewport; history search and pagination remain available |

The upload journey exercised partial upload success and a rejected file, extraction/review, saved draft restoration, explicit approval, preview, final import, separate creation/tag results, and opening/searching History. Browser fixtures stub Mealie and use synthetic records. The history recovery journey verified disclosure, confirmation gating, durable attempts, and Mealie identity. The existing D2-only Review browser journey passed at 1440, 768, and 320 widths without writing review decisions.

### Browser harness findings

- The D3 upload journey initially exposed two harness gaps: the narrow Review journey needed an explicit Recipe-pane selection, and History needed opening after the new disclosure was introduced. Both are corrected in the harness.
- `browser_history_recovery_acceptance.mjs` initially observed a transient page because navigation had not completed; it now waits for the destination and passes.
- Separate, unchanged Review acceptance runs remain imperfect: the D2 correction run timed out opening an existing issue-details expander, and the full Review acceptance run expected revision 0 after an earlier suggestion flow had advanced the fixture to revision 1. D2-only and the D3 upload-to-approval journey pass; these unrelated Review harness cases are recorded for follow-up rather than treated as D3 product failures.

## Verification

Focused AppTests: Upload **5 passed**; Import & history **9 passed**. Complete offline suite, browser fixture tests, Python compilation, CLI help, JavaScript syntax checks, and `git diff --check` are recorded in the delivery summary after final verification. No live Gemini or Mealie calls were made.

## Accessibility review limits

The rendered browser checks cover layout/reflow at 320 CSS px, visible workflow labels/headings, the text-described rejected-file state, visible Review focus, and the browser accessibility names/roles used by the journey. They do not establish WCAG conformance. The targeted review did not compute color contrast across all UI states, inspect every interactive target, test 200% text resizing, or complete a keyboard-only walk through every Upload and recovery control. Screen-reader behavior and accessible authentication are also unverified. See the viewport reports for measured dimensions and focus details.

For explicit scope accounting, the two D3 screens were reviewed separately against these 18 criteria. “Checked” is limited to the stated rendered states; “not verified” is not a pass.

| SC | Upload | Import & history |
| --- | --- | --- |
| 1.1.1 Non-text content | Not verified: all informative-image alternatives require DOM review | Not verified: all informative-image alternatives require DOM review |
| 1.3.1 Info and relationships | Not verified: heading appearance was inspected, full DOM relationships were not | Not verified: heading appearance was inspected, full DOM relationships were not |
| 1.4.3 Contrast minimum | Not verified: ratios were not measured | Not verified: ratios were not measured |
| 1.4.4 Resize text | Not verified: no 200% text-size capture | Not verified: no 200% text-size capture |
| 1.4.10 Reflow | Checked: 320px viewport report shows document width 320 with no horizontal overflow | Checked: 320px viewport report shows document width 320 with no horizontal overflow |
| 1.4.11 Non-text contrast | Not verified: all borders, icons, and states were not measured | Not verified: all borders, icons, and states were not measured |
| 2.1.1 Keyboard | Not verified: no keyboard-only completion of Upload | Not verified: no keyboard-only completion of import and recovery |
| 2.1.2 No keyboard trap | Not verified: no full keyboard-only component exit check | Not verified: no full keyboard-only component exit check |
| 2.4.4 Link purpose | Not verified: all links and context were not audited | Not verified: all links and context were not audited |
| 2.4.6 Headings and labels | Checked: visible workflow headings and upload labels describe their purpose | Checked: visible Ready to import, History, search, and form labels describe their purpose |
| 2.4.7 Focus visible | Not verified for Upload controls; Review focus evidence is a different workspace | Not verified for import and recovery controls; Review focus evidence is a different workspace |
| 2.4.11 Focus not obscured | Not verified for Upload controls; no matching focused Upload state | Not verified for import and recovery controls; no matching focused state |
| 2.5.8 Target size | Not verified: every pointer target and spacing exception were not measured | Not verified: every pointer target and spacing exception were not measured |
| 3.2.2 On input | Not verified: no complete change-of-setting interaction audit | Not verified: no complete selection/tag-change interaction audit |
| 3.3.1 Error identification | Checked: rejected-file state presents a textual error alongside the file outcome | Not verified: no import error-state screenshot in this audit set |
| 3.3.2 Labels or instructions | Checked: upload/extraction controls have visible labels/instructions | Checked: tag, preview, search, and page controls have visible labels |
| 3.3.8 Accessible authentication | Not verified: authentication flow is outside these screens | Not verified: authentication flow is outside these screens |
| 4.1.2 Name, role, value | Not verified: journey checked selected accessible names, not every control/state | Not verified: journey checked selected accessible names, not every control/state |

## Recommended D4 scope

Run a synthetic usability and performance walkthrough for one person managing 100+ batches, recipes, and history entries. Include interrupted extraction, retry, uncertain-create reconciliation, and tag repair; measure navigation and recovery effort; add 200% text, keyboard-only, contrast, target-size, and screen-reader checks. Keep live service compatibility and any range-import enablement outside D4 unless separately authorized.
