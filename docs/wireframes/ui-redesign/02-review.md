# Review workspace interaction specification

Proposed synthetic content. R1 owns durable semantics; R2 owns presentation. No real screenshots are claimed.

## V1 — Desktop review, 1440×900 intent

```text
Recipe Importer          Uploads           Review [active]           Import & history
Batch [Weeknight cookbook ▾]  [To review ▾]  [Queue: 32]     6 need attention · 8 ready
------------------------------------------------------------------------------------
[Fried Rice                                    ]  Ready to review   3 of 32
Saved locally · revision 4     No blocking issues
[Previous] [Save draft] [Skip for now]                          [Approve & next]
┌─ Source ──────────────────────────────────┐ ┌─ Recipe ─────────────────────────────┐
│ cookbook.pdf                             │ │ Ingredients            [Bulk edit] │
│ [‹] Page [2] of 12 [›]  [Fit width ▾] [+] │ │ [2 cups cooked rice           ] [⋯] │
│                                          │ │ [1 egg                        ] [⋯] │
│  FRIED RICE                              │ │ [1 tbsp oil                   ] [⋯] │
│  2 cups cooked rice                      │ │ [+ Add ingredient]                 │
│  1 egg                                   │ │                                    │
│  1 tbsp oil                              │ │ Instructions           [Bulk edit] │
│                                          │ │ 1 [Warm oil in a pan.           ] ⋯ │
│  Warm oil in a pan.                      │ │ 2 [Fold in rice and egg.        ] ⋯ │
│  Fold in rice and egg.                   │ │ [+ Add step]                       │
│                                          │ │ Optional details ▾                 │
│ [Original OCR text ▾]                    │ │ [Yield ...] [Description ...]      │
└──────────────────────────────────────────┘ └────────────────────────────────────┘
Source, extraction and review history ▾
```

Chrome target ≤200px; source/editor occupy the main viewport. Exact width/scroll/sticky behavior depends on native feasibility and browser checks. Show only one action strip if reliably reachable; top/bottom native fallback may duplicate actions for long content, but never queue navigation and titles throughout the page. Current 650px containers are not a responsive specification.

Title is a supported editable field; changing it retains original candidate ID. Source page controls bound to 1–N. At first/last page disable respective direction. Candidate advance does not increment source page automatically; prefer known evidence location, else restore last page and label unknown location. Zoom must allow inspecting a fraction with a keyboard exit-to-fit action. Original OCR text is evidence, not cleaned authoritative fields.

## V2 — Queue drawer / selection

```text
Review queue                                                      [Close]
Batch [Weeknight cookbook ▾]     [Search title or source          ]
[To review] [Needs attention] [Ready to import] [All recipes]
Source [All sources ▾]
  Fried Rice                         Ready to review   cookbook.pdf · page 2
> The Best Caramel Apples            Needs attention   15 text concerns
  Cranberry & Caramel Date Bars       Needs attention   1 critical quantity
  Imported Recipe                    Imported          Read-only
[Previous page]     1–25 of 38       [Next page]
```

Click a row calls dirty-save guard before selection; no title-only identity. Enter selects from keyboard; close returns focus to Queue. Search/filter does not auto-approve or discard. Current queue snapshot/order persists for Next/Previous; state changes caused by saving do not randomly jump to a different first candidate. Exact source pages shown only when recorded. Protected rows appear in All recipes with read-only detail.

## V3 — Precise row and step actions

```text
Ingredient 2 [1 egg                                              ] [Ingredient actions ▾]
Menu: Insert below · Move up · Move down · Remove

Step 3
[ Stir until the caramel coats the apples evenly.                    ] [Step actions ▾]
[ Let the excess drip back into the bowl.                            ]
Menu: Insert below · Move up · Move down · Merge with next · Remove
[Undo last edit]
```

Menus illustrated by ⋯ in desktop frame have accessible labels, not unnamed dots. Move at boundary disabled; add focuses new field. Remove/merge keep undo revision. Entries retain original strings and ordering. Merge preview preserves text exactly and uses a deliberate separator; it never invents cooking instructions. Splitting uses Bulk block boundaries in native first release, not unsupported cursor capture. After mutation focus goes to surviving/new row; error retains current row.

## V4 — Bulk correction and step preview

```text
The Best Caramel Apples             Needs attention       Draft saved · revision 7
Source remains alongside editor; no separate browser tab required.

Ingredients — bulk edit
One ingredient per line. Suspicious text is retained until you remove it.
┌─────────────────────────────────────────────────────────────────────┐
│ 6 apples                                                           │
│ 2 cups caramel                                                     │
│ %                                                                  │
│ Noy 0 9 2021                                                       │
│ ??                                                                 │
│ ... 12 more synthetic OCR fragments ...                            │
└─────────────────────────────────────────────────────────────────────┘
Preview: 17 ingredients → 2 after your changes · 15 removed
[Apply ingredients] [Cancel bulk edit]

Instructions — bulk edit
Blank lines separate steps. Review the step boundaries before applying.
┌─────────────────────────────────────────────────────────────────────┐
│ Wash and dry apples.                                               │
│                                                                    │
│ Warm caramel in a bowl.                                            │
│                                                                    │
│ Dip each apple and let excess drip back.                           │
└─────────────────────────────────────────────────────────────────────┘
Preview: 12 fragments → 5 steps (show full proposed blocks)
[Apply instructions] [Cancel bulk edit]
```

Frame values illustrate synthetic cleanup; no hidden parser removes text. Selection/delete/paste can repair many lines in one edit. Apply changes only the chosen section; pending bulk text is draft material and cannot approve until applied/validated. Cancel restores pre-bulk structured content, preserving raw history. Refresh restores confirmed raw bulk draft and mode. If parsing alters blank-block boundaries, show before/after and require Apply; never quietly reinterpret an approved recipe.

## V5 — Field-local uncertainty and complete issue inventory

```text
Cranberry & Caramel Date Bars       Needs attention      [Issues: 2]
Ingredient 1 [dates                         ] [Ingredient actions ▾]
! Critical — the quantity is cut off in the source
Evidence: page 4, left column (only if recorded)       [Inspect source]

┌─ Unverified suggestion — quantity for dates ────────────────────────┐
│ 1 cup dates                                                        │
│ Why: plausible completion; not confirmed by this scan               │
│ Reported confidence: low · Uncertainty: quantity is cropped          │
│ [Use suggestion] [Edit suggestion] [Reject]                         │
└────────────────────────────────────────────────────────────────────┘
Enter a correction from the source [ ... ] [Record source correction]
Or leave this issue unresolved; Skip for now saves your work.

Issues (complete inventory)
1 Critical quantity → Ingredient 1
2 Proposal without a current field target → [Choose target] [Defer]
[Approve & next disabled] Resolve the critical quantity before approving.
```

Use/Edit previews exact replacement, then writes explicit decision and linked resolution through R1. For missing-source value label resultant field **Human-approved completion**. Reject writes rejected disposition; source diagnosis remains unless independently resolved. Defer leaves pending/unresolved. Multiple proposals shown individually even on same target; user decides among them, no automatic ranking. Nonindexed title/yield/description/whole-section proposals appear in Issues and at relevant field. A moved/deleted row cannot make a proposal attach to whatever now occupies the old index.

Resolution content must indicate corrected-from-source versus explicitly human-provided completion. Removed OCR artifacts can be resolved through **Not recipe content** only after an explicit source comparison; retain the original line and finding in history. Row deletion alone never clears it. A whole-recipe approval is not a field-resolution shortcut. Source location unknown says so; no auto-generated highlights or assumed page 1.

## V6 — Save, failure, navigation and approval states

```text
Saving locally…                 [Approve & next temporarily unavailable]
Saved locally · revision 8      [Approve & next]

Draft was not saved. Your current text is still here.
Last confirmed save: revision 7
[Retry save] [Stay here] [Discard unsaved changes and continue]

Draft saved; approval not recorded.
[Retry approval] [Stay here]    (do not create another recipe or lose draft)

Another session saved a newer revision.
Your current edits are retained. [Review differences] [Reload latest]
```

Approving flushes/saves content, validates, records exact consent, readbacks and advances only after success. Any later content mutation invalidates approval. Save & next is secondary and never approves. Skip saves and advances without rejection; deferred items stay resumable. All top navigation/filter/queue/previous/next actions use the same guard. Browser refresh restores only confirmed committed input; typed but uncommitted native text is clearly unsaved and requires a tested exit guard. No promise of every-keystroke durability.

## V7 — Empty, end, missing source and protected records

```text
No recipes match these filters.                     [Clear filters]
No extracted recipes yet.                           [Open Uploads]

You've reviewed this queue.
8 ready to import · 2 deferred · 1 source still needs extraction
[Review import selection] [Return to deferred] [Open source activity]

Original source unavailable — your saved recipe is retained.
[Locate matching source] [Show original OCR text if available]

Imported — this recipe is read-only here.
[View in Mealie] [View import history]                No edit/approve controls
Check Mealie — the previous create result is uncertain.
[Open reconciliation]                               Creation remains blocked
```

Unknown protection due to unreadable history is a visible safety block. Missing source permits editing/saving but disables new approval with **Locate the original scan before approving**. Original OCR text alone does not remove this gate. An unchanged previously approved snapshot retains its recorded consent; editing it requires reapproval after source access is restored. End of filtered queue does not claim all batches completed. Previous from end returns to last item; no forward wrap.

## V8 — Narrow / text-expanded mode

```text
Recipe Importer      [Navigation ▾]
Weeknight cookbook   [Queue]
Fried Rice · 3 of 32
Saved locally · revision 4
[Show recipe] [Show source]
Ingredients
[2 cups cooked rice              ]
[Ingredient actions ▾]
[1 egg                           ]
[Ingredient actions ▾]
Instructions
1 [Warm oil in a pan.            ]
  [                             ]
[Step actions ▾]
[Save draft] [Skip for now]
[Approve & next]
```

Stack or focus-switch when panes no longer support readable editing. No fixed-height nested editor scroller at 320px, actions wrap and stay in document flow. Focus switch returns to last relevant field/page; saved state always available. Source zoom deliberate panning is distinct from accidental page horizontal scroll. Screenshot at narrow width and 200% text plus keyboard/DOM evidence required before R2 acceptance.
