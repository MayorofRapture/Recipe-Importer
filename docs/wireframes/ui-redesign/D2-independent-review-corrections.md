# D2 independent review corrections

**Status:** Corrections implemented; awaiting independent review.
**Baseline:** `e6c4faffcdcd95b35b7d402a54aebd110db2a7a9`
**Scope:** Correct finding identity projection and pending proposal labels. Preserve D2 layout and D1 behavior.

## F-01 — Finding identity after resolution

The defect was reproducible. The D2 AppTest resolved the first of two row findings and then failed to find the second row's contextual diagnosis: the current diagnostic list had shrunk, so its current index no longer matched the immutable registration issue ID. `_resolve_finding()` also selected the first finding binding by field path, which could resolve the wrong issue when several findings shared a path.

Contextual rendering and resolution now locate one binding using its immutable registered `original_issue` payload. Row placement then follows the binding's immutable source target ID. Legacy bindings without `original_issue` are accepted only when their field path and detail identify exactly one target. Missing or ambiguous bindings are omitted from inline placement and cannot be resolved by position; the finding remains visible in the general Issues view with an explanation. Registration identities and historical events are unchanged.

Synthetic Chromium evidence shows the second issue beside `cranberries` before and after resolving the first issue, then beside that same row after moving it to position 1. Defer leaves it unresolved. The only recorded resolution in the fixture has the original issue ID `finding:0:ingredients[0]`.

- [Before resolution](evidence/d2-f01-before-resolution.png)
- [After resolving the first finding](evidence/d2-f01-after-first-resolution.png)
- [After reordering the remaining row](evidence/d2-f01-after-row-reorder.png)

## F-02 — Proposal provenance and review status

Proposal summaries now show provenance and review state as separate labels. Both pending AI-inferred and source-supported proposals are marked `Unverified`; the source-supported proposal remains disabled until the existing source-verification checkbox is selected. After an explicit decision, the summary retains provenance and reports the applied/rejected state. No decision or authority path changed.

AppTest covers pending AI-inferred and source-supported labels, the source verification gate, and the source-supported applied state. Existing synthetic Chromium journey covers AI-inferred reject, edit, and accept state transitions; the F-01 journey verifies Defer leaves the source issue open.

## Verification

- Focused Review AppTests: **21 passed**.
- Full offline suite: **620 tests, 2 skipped; 1 timing-sensitive unrelated failure**. `test_range_job_requires_cloud_consent_and_completed_candidates_are_import_blocked` observed `running` after its fixed five-second worker join. The same test passed in isolation immediately afterward; no D2 code or test path was involved.
- Synthetic Chromium F-01 flow: first finding resolution, remaining row association, row reorder, and Defer all passed.
- Synthetic Chromium D2 view: issue/proposal labels at 1440px, 768px, and 320px; no horizontal overflow.
- Existing synthetic Review journey: proposal Reject/Edit/Accept, Save, Approve, and fake import passed.
- D1 fragment-state journey: dirty header refresh, approval disable/enable on incomplete/complete content, no full-app rerun or draft write during edits.
- D1 responsive journey: 1920px, 1440px, 768px, 320px, and 200% text; Save and Approve remained visible, no horizontal overflow, pane/tab/unsaved-edit state retained.
- Python compilation, credential-free CLI help, syntax checks for all 11 JavaScript modules, browser fixture runner tests (**10/10**), and `git diff --check` passed.
- No Gemini or Mealie calls were made; browser fixtures and fake import used synthetic local data.

## Limits and stop point

The full suite had one unrelated timing-sensitive failure; its isolated retry passed. Screenshots and local AppTests do not establish screen-reader behavior or full WCAG conformance. No live-service behavior was tested.

Stop after D2 corrections for independent review. D3 has not started. PR #3 remains open and unmerged.
