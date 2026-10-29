# D1 — Review workspace layout implementation

**Status:** Implementation complete; awaiting independent review.  
**Date:** 2026-10-26  
**Scope:** Review viewport, action placement, focus, and library-path presentation only.

D1 preserves the existing ReviewStore, DraftStore, candidate identities, source provenance, approval rules, import projection, and Mealie behavior. The implementation changes presentation and keeps both narrow-screen panes mounted so Streamlit widget state survives switching. D2 issue/proposal placement and D3 Upload/Import refinements remain deferred.

## Before and after

The UX-0 baseline audit recorded the following at 1440×900: the workspace began around y=348, Save draft around y=839, and Approve & next below the fold around y=933. At 768px the panes were only about 280–350 CSS px wide and the actions were below the first viewport. At 320px source and editor comparison was inefficient.

The D1 synthetic Chromium run measured the updated layout:

| Viewport | D1 result |
| --- | --- |
| 1920×1080 | Both panes visible: Source 772px, Recipe 947px. Save/Approve at y=347; no horizontal overflow. |
| 1440×900 | Both panes visible: Source 556px, Recipe 683px. Save/Approve at y=347; no horizontal overflow. |
| 768×900 | One pane visible at a time, 726px wide. Save/Approve at y=407. |
| 320×900 | One pane visible at a time, 278px wide. Save/Approve at y=542. |
| 200% text-size emulation | At a 1440×900 viewport, root text size was raised from 16px to 32px. No horizontal overflow; Save/Approve remained present at y=626. |

All action positions are within the initial viewport. At 768px and 320px, the browser switched between Source and Recipe with an unsaved ingredient edit, page 2, Inspect zoom, and the Instructions tab retained. The selected candidate remained Fried Rice in the isolated fixture. No document horizontal overflow occurred at any tested width.

## Implementation and authority checks

- Review now identifies the active recipe, saved revision/unsaved state, status, and queue position above the workspace. Queue and primary actions are in normal document flow ahead of the panes.
- Save and Approve retain candidate-specific widget keys and call the existing `_save` / `_approve` paths. The main page synchronizes editor widget values into the existing buffer before those actions render. No review or import service was changed.
- AppTest verifies an edit made immediately before Approve is recorded in the saved revision and approval references that exact revision. Existing tests cover no-op Save feedback, dirty approval blocking, and save-conflict behavior.
- The full offline suite passed **612 tests, with 2 skipped**. The synthetic browser journey entered both instruction fields, waited for the editor fragment to settle, saved revision 1, approved that revision, and completed a fake Mealie import with the saved instructions.
- The library caption is now short; its exact selected path and conflicting-library diagnostic remain available in the optional Library troubleshooting expander. Library resolution and fixture attestation are unchanged.

## Evidence

Screenshots use isolated synthetic data. The browser harness verifies fixture attestation before hiding its run-specific caption for capture.

- [1920×1080 Source](evidence/d1-review-source-1920x1080.png)
- [1440×900 Source](evidence/d1-review-source-1440x900.png)
- [768×900 Source](evidence/d1-review-source-768x900.png)
- [768×900 Recipe](evidence/d1-review-recipe-768x900.png)
- [320×900 Source](evidence/d1-review-source-320x900.png)
- [320×900 Recipe](evidence/d1-review-recipe-320x900.png)
- [200% root text-size emulation](evidence/d1-review-text-200-percent.png)
- [Keyboard focus on the source page control](evidence/d1-review-keyboard-page-focus.png)

Browser evidence also checks actual pane visibility and widths, action positions, document overflow, visible input values after pane changes, and the recipe tab selection. Pane switching leaves keyboard focus on the active switcher control. The full existing browser acceptance journey separately checks accessible names/roles for source controls and review actions, source text exposure, keyboard navigation, save/approval transitions, and focus after candidate navigation.

The full synthetic Chromium journey also recorded the source heading/text/page control in the accessibility tree and measured 13.33:1 text contrast for source text and provenance in the captured state. The keyboard path to the Page control took 15 Tab presses; the focused control has a visible teal border in the screenshot. Focus returned to Recipe title after candidate navigation and after Approve & next.

## Limits and deferred work

- Automated DOM and accessibility-tree checks are not screen-reader testing or a complete WCAG conformance audit.
- D1 browser evidence does not establish live Gemini or Mealie behavior; no live services were contacted.
- The 200% check raises the document root font size; it is not a browser-native zoom setting. Full keyboard-only task completion, focus after opening and closing Queue or changing recipe tabs, focus after a browser-level injected save conflict, and screen-reader behavior remain unverified.
- Issue/proposal co-location, Upload layout, Import/history layout, and broader typography refinements are deferred to D2/D3 or later review.

Stop after D1 for independent review. Do not begin D2.
