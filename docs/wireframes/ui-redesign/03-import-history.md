# 03 — Import and history

**Status: proposal.** Keep existing exact-revision eligibility, preflight, fake-service boundary, import intents, tag outcomes, and reconciliation behavior.

## Proposed layout

```text
Import & history

READY TO IMPORT
Only approved, complete recipe revisions appear here.
[Select eligible recipes ▾]
Recipes to import: [3 selected ▾]
Mealie tag: [Sunday recipes________]  Existing tags are preserved.
[Preview import]

Preflight
3 recipes will be sent · 1 existing tag will be kept · 2 need tag creation
[Review selected recipe details]                 [Import 3 recipes]

CREATE / TAG OUTCOME
✓ Recipe A created in Mealie · tag applied
! Recipe B created · tag needs repair (recipe is protected from re-creation)
? Recipe C outcome uncertain · Check Mealie before any retry

HISTORY
[Search title or source________________] [Status ▾] [Page __]
Recipe C · Check Mealie · source.pdf · [Reconcile]
Recipe B · Imported; tag needs repair · source.pdf · [Repair tag]
Recipe A · Imported · source.pdf · [Open details]
```

The layout places the current preflight above history, with separate headings and outcome language. Search and pagination find prior work but do not turn it into a new import selection. Reconciliation controls remain tied to the protected attempt and existing service; no “retry import” shortcut appears for an uncertain result.

## Small screen

Stack the selection, tag, preflight, and history sections vertically. Keep selected count and the Preview action visible before the import decision. Outcome cards name creation and tag status separately. Controls wrap, and no table requires horizontal scrolling.

## User value and regression risk

The distinction lets the user see what is newly eligible without confusing it with an old or uncertain attempt. This is a small-to-medium presentation change with high regression consequence: exact approval binding, tag preservation, uncertain-result protection, and range exclusion must remain unchanged. Confirm behavior with mocked service outcomes and an isolated rendered journey; live Mealie remains a separate release gate.
