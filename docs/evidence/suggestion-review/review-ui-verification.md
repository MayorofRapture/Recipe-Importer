# Suggestion review UI verification

Captured 2026-10-17 on the development branch. Browser work used only the synthetic fixture library, an injected fake proposer, and a simulated Mealie response. No Gemini or production Mealie request was made.

## User-flow checks

- The Instructions panel explains what text is sent and asks for affirmative cloud-request consent before creating a durable background job. Pending content stays separate from the authoritative recipe and shows the target, current/proposed wording, difference, rationale, and uncertainty.
- A generated completion can be accepted or edited explicitly. Applying one leaves any matching critical source issue unresolved; the user must take a separate source-confirmation action before approval becomes available.
- Declining an optional generated completion resolves only that proposal. An independent critical issue remains an approval blocker.
- Editing the recipe while generation is pending makes its proposal stale. The UI compares the saved proposal with the current wording and offers an explicit current-revision action only when the stable target still exists. Target-missing proposals cannot be applied. Stale content can instead be discarded before another request with fresh consent.
- A queued request is discovered after a new Streamlit session. Resume and Cancel are explicit actions; uncertain provider outcomes are not automatically retried.
- Browser journeys also exercised Skip, Back, return from the persistent Skipped list, a simulated import preview/history flow, and a 320 CSS-pixel viewport. The selected performance screenshots show the current review shell, editor, source preview, tabs, bulk editor, and navigation controls.

Focused automated UI/revision/suggestion checks passed 42/42 after the final idle-poll change, plus the fresh-consent-after-completion AppTest passed 1/1. The final full offline suite passed **343 tests in 184.300 seconds** with `.venv\Scripts\python.exe -m unittest discover -s tests` and `TEMP`/`TMP`/`TMPDIR` set to `.test-tmp`. Targeted Python compilation, CLI help, JavaScript syntax, and whitespace checks passed.

## Actual Chromium interaction measurements

The measurements are from a fresh no-hook Streamlit server on port 8920 and a local Chromium headless-shell session controlled through CDP. The fixture contains 500 synthetic recipes, 700 import-state history rows, 521 review events, 50 UI drafts, and 75 queue events. The Fit source preview loaded at 1200 pixels wide. Each action used five samples; the first sample is included. Values are browser interaction latency, not provider latency or a service-level guarantee.

| Interaction | Median | P95 / worst | Per-action work |
| --- | ---: | ---: | --- |
| Ingredients/Instructions tabs | 176 ms | 186 ms | 0 full-page runs, catalog loads, source hashes, ledger reads, previews, or draft saves |
| Open bulk editor | 175 ms | 206 ms | 0 full-page runs, catalog loads, source hashes, ledger reads, previews, or draft saves |
| Skip to next recipe | 585 ms | 1205 ms | +1 full-page run, catalog, source hash, preview; +4 ledger reads; 0 draft saves |
| Back to prior recipe | 539 ms | 1143 ms | +1 full-page run, catalog, source hash, preview; +4 ledger reads; 0 draft saves |

The `durable_writes` diagnostic counts draft-save attempts, not every filesystem write. Navigation separately persists queue/visit events and the resume cursor; these records have no approval authority.

The first Skip sample and worst Back sample are retained in `browser-performance.json`; they were not dropped as outliers. Tabs and bulk-edit samples showed no additional suggestion-poll fragment executions beyond the action's own editor fragment work while no AI request was active. The earlier Runs with multiple leftover browser targets were superseded and are excluded from this table.

A one-shot real-server profile taken before the preview cap showed high cumulative image serialization cost. It is kept separately as `diagnostic-profile-before-preview-cap.json`; async profile totals are non-additive, and that profile is diagnostic evidence rather than a final latency comparison. The accepted Fit raster cap is 1200 by 1600 pixels, with larger explicit detail levels available in the UI.

Detailed sample counters are in `browser-performance.json`. The rendered captures are `review-tabs-1440x1000.png`, `review-bulk-edit-1440x1000.png`, `review-skip-1440x1000.png`, and `review-back-1440x1000.png`. Workflow captures include `ai-suggestion-pending.png` (consent-ready Instructions panel), `ai-stale-rebase-comparison.png` (current/proposed comparison and explicit rebase prompt), `ai-applied-critical-open-current-800x1000.png` (human-approved suggestion while an independent critical finding still blocks approval), and `persistent-skipped-list.png`.

## Visual accessibility review

The current 1440 by 1000 screenshots show distinct source and recipe regions, visible field labels, named tabs, and clear Skip/Back controls. The controls shown are visibly larger than 24 by 24 CSS pixels. A separate browser reflow check at 320 CSS pixels reported no horizontal overflow. These checks found no obvious visual blocker in the sampled states; they do not establish WCAG conformance. The screenshot/reflow review relates to [WCAG 2.2 SC 1.4.10 Reflow](https://www.w3.org/TR/WCAG22/#reflow) and [SC 2.5.8 Target Size (Minimum)](https://www.w3.org/TR/WCAG22/#target-size-minimum), but did not verify either criterion end to end.

Not verified in this evidence set: measured text/icon contrast ([SC 1.4.3](https://www.w3.org/TR/WCAG22/#contrast-minimum), [SC 1.4.11](https://www.w3.org/TR/WCAG22/#non-text-contrast)); keyboard traversal and absence of traps ([SC 2.1.1](https://www.w3.org/TR/WCAG22/#keyboard), [SC 2.1.2](https://www.w3.org/TR/WCAG22/#no-keyboard-trap)); focus visibility/not obscured ([SC 2.4.7](https://www.w3.org/TR/WCAG22/#focus-visible), [SC 2.4.11](https://www.w3.org/TR/WCAG22/#focus-not-obscured-minimum)); accessible names/roles/states ([SC 4.1.2](https://www.w3.org/TR/WCAG22/#name-role-value)); semantic relationships; non-text alternatives; behavior on input; and an error-state label audit ([SC 3.3.2](https://www.w3.org/TR/WCAG22/#labels-or-instructions)). Screenshots alone cannot establish these properties. A complete DOM/accessibility-tree and keyboard audit remains unverified.

## Scope limits

All recipe text, scans, generated suggestions, and server responses in this report are synthetic. Real Gemini quality, quota behavior, production Mealie compatibility, and live import outcomes were not tested. The browser run did not include provider-generation latency. The full suite result should be taken from the separate final run, not inferred from the focused test count above.
