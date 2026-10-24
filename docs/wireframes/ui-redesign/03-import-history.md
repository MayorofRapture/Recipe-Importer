# Import, history and maintenance

Proposed controls; fake service scenarios only until explicitly authorized live verification. R3 owns this workflow.

## I1 — Ready selection

```text
Recipe Importer       Uploads       Review       Import & history [active]
Batch [Weeknight cookbook ▾]             [Ready] [History / Needs attention]
--------------------------------------------------------------------------------
Ready for Mealie
8 approved recipes · 2 need review · 1 already imported
[Select all eligible]                                      0 selected
  □ Fried Rice                     Approved revision 4     cookbook.pdf
  □ The Best Caramel Apples        Approved revision 9     apples.jpg
  — Cranberry & Caramel Date Bars   Needs attention          [Review]
  — Imported Recipe                Imported                 [History]
Mealie tag (optional) [Weeknight scans             ]
Only selected newly created recipes receive this tag. Existing tags are preserved.
[Review import selection]  disabled until selection exists
```

Ready includes exact approved snapshot, not any complete extraction. Default selection empty, scoped Select all includes only current eligible revisions. Changing batch/filter does not carry hidden selections across scope without explicit disclosure; show/clear the selection before proceeding. Tag change affects this operation only; no historical retag.

## I2 — Consequential preflight

```text
Import 2 recipes into Mealie?
Destination: configured host (no token)      Tag: Weeknight scans
Fried Rice · approved revision 4
The Best Caramel Apples · approved revision 9
Excluded: 1 imported, 1 needs review, 0 uncertain
Recipe creation and tag results will be recorded separately.
[Import 2 recipes] [Back to selection]
```

Confirm records local operation identity/intent before dispatch. Revalidate revision/protection at dispatch; stale entries removed with explanation, not sent using old consent. Repeat click/rerun uses same operation, never duplicate worker. Import service sends only selected snapshots; no extraction/sibling import. Once started, **Stop after current recipe** means no new scheduling; it cannot undo in-flight creation.

## I3 — Partial results / recoverable tagging

```text
Import results — operation 12
Recipe                      Recipe creation             Mealie tag       Next action
Fried Rice                  Created ✓                   Added ✓          View in Mealie
The Best Caramel Apples     Created ✓                   Failed           Retry tag only
Cranberry Date Bars         Not sent: needs review      Not attempted    Review
Another Recipe              Rejected by server          Not attempted    Retry selected failure
Timeout Recipe              Check Mealie                Unknown          Reconcile

2 created · 1 tag needs attention · 1 definite failure · 1 uncertain
[Open history] [Continue review]          Details ▾
```

Do not collapse Created/tag-failed into failed import. Tag retry uses confirmed slug and original intended tag, preserves other tags and performs zero create POSTs. Definite create failure retry rechecks approval/protection. Unknown create cannot retry; not sent is not failure. If creation succeeds but local record cannot be written, show uncertainty and halt new sends; never falsely report definite rejection.

## I4 — Independent history and uncertainty

```text
History / Needs attention       [Search recipe or source] [Result filter ▾]
Recipe               Source              Created             Tag        Action
Imported Recipe      cookbook.pdf        Confirmed · 10:12   Added      View
Caramel Apples       apples.jpg          Confirmed · 10:14   Failed     Retry tag
Timeout Recipe       timeout.jpg         Outcome uncertain   Unknown    Check Mealie
Older import         Cache unavailable   Confirmed · prior   —          View history

Check whether Mealie received Timeout Recipe
The request stopped before a dependable result was recorded. Do not repost yet.
[Open Mealie] [Show attempt details]
Supported reconciliation (only after contract validation):
[Link confirmed existing recipe] [Record confirmed not created] [Leave unresolved]
```

History uses import ledger even with no extraction cache. Existing recipe link only rendered when actual slug/destination can be safely formed. Reconciliation is evidence-backed, append-only and scoped; search not finding a title is not proof of absence. Until the installed contract supports reliable verification, replace reconciliation mutation buttons with manual check guidance and keep creation blocked. Never advise deleting an UNCERTAIN record.

Retag/backfill is a separate explicit scope preview, not a generic “fix batch” button. User sees which existing slugs/tag will change and consents before any update. Updating already-imported recipe content is outside this redesign.

## I5 — Empty, interrupted and completed

```text
No approved recipes yet.                         [Review recipes]
No import history yet.                           [Open Ready]
No matches for this filter.                      [Clear filters]

Import interrupted
3 recipes confirmed created, 1 started without confirmed result, 4 not sent.
[Check uncertain result] [Resume remaining eligible recipes]

All selected recipes created and tagged.
[View in Mealie] [Continue review]
```

Resume excludes confirmed and uncertain candidates; started-without-result remains uncertain after restart. Overall success only when requested create/tag outcomes are confirmed; optional no-tag operations explicitly say tagging not requested. Distinguish user-deferred review from completed import.

## Accessibility and responsive contract

Selection checkboxes have recipe-specific labels; disabled items explain why. Preflight focus stays within the decision surface and Back restores previous selection focus. Result updates announce full context without changing selected row. Tag retry/reconciliation labels include recipe context for assistive technology. Desktop table becomes labeled stacked outcome cards at narrow width, with separate Creation/Tag labels retained. Never rely solely on color/checkmarks. Browser evidence must include fake happy import, tag-only repair, uncertain-result recovery and missing-cache history.
