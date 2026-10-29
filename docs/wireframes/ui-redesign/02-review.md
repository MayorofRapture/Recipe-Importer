# 02 — Review workspace proposal

**Purpose:** keep source evidence and recipe editing in one working context, shorten repetitive corrections, and keep decisions visible. Preserve existing review services and authority.

## Wide desktop — 1440px and above

```text
┌───────────────────────────────────────────────────────────────────────────┐
│ Upload     Review     Import & history                   [Help / details] │
├───────────────────────────────────────────────────────────────────────────┤
│ Fried Rice       Ready to review · 1 of 3      Saved draft · rev 2         │
│ [Queue ▾] [Back] [Skip]                 [Save draft] [Approve & next]      │
├────────────────────────────────┬──────────────────────────────────────────┤
│ SOURCE                          │ RECIPE                                   │
│ cookbook.pdf · page 1 of 2      │ Fried Rice                               │
│ [Page ▾] [Fit ▾] [−] [100%] [+]│ [Yield ▾]                                │
│                                │                                          │
│      readable source scan      │ INGREDIENTS     INSTRUCTIONS             │
│                                │ 1. 2 cups rice  1. Cook rice.             │
│ [Selectable source text ▾]     │ 2. 1 egg       2. Fold in egg.           │
│                                │ [Add] [Bulk edit]                         │
│                                │                                          │
│                                │ ⚠ Quantity unreadable                     │
│                                │ Source note · “1/2 cup” proposal pending │
│                                │ [Review proposal]                          │
└────────────────────────────────┴──────────────────────────────────────────┘
```

The source and recipe panes should receive most of the first viewport. Queue filters, extraction lineage, raw diagnostics, and full issue histories open from compact controls. Keep Save/Approve adjacent to the candidate identity and repeat a normal-flow action at the editor end if a long recipe scrolls. Do not implement a sticky bar if it obscures keyboard focus or causes widget remounts.

## Narrow screen — around 768px and below

```text
┌────────────────────────────────┐
│ [Destinations ▾]        [Queue] │
├────────────────────────────────┤
│ Fried Rice · 1 of 3             │
│ Needs attention · Draft saved   │
│ [Save draft] [Approve & next]   │
├────────────────────────────────┤
│ [Source]  [Recipe]              │
├────────────────────────────────┤
│ SOURCE VIEW                     │
│ cookbook.pdf · page 1 of 2      │
│ [Page ▾] [Fit ▾] [Zoom controls]│
│                                │
│     source scan or text         │
│                                │
│ [Issue: quantity unreadable]    │
│ [Open affected field]           │
└────────────────────────────────┘

Recipe view:
┌────────────────────────────────┐
│ INGREDIENTS                     │
│ 1. [2 cups rice             ]   │
│ 2. [1 egg                   ]   │
│ [Add ingredient] [Bulk edit]    │
│ ⚠ Quantity unreadable           │
│ [Show source] [Review proposal] │
└────────────────────────────────┘
```

At widths where both panes cannot remain readable, Source/Recipe is a visible tab or segmented control, not a hidden gesture. Opening an issue from Recipe view switches to the affected recipe field; Show source returns to the corresponding page. Keep the title, queue position, save state and primary actions in view. At 320 CSS px, controls wrap to one column with no horizontal page scroll.

## Interaction and state annotations

1. **Candidate line:** title, queue position, source name, and plain-language state. Keep local path/hash out of the default view.
2. **Queue button:** opens search/filter/needs-attention list. Selecting another recipe first preserves the committed draft or explains why it cannot be saved.
3. **Source view:** page and zoom are bounded to available pages. Source text remains labeled as selectable OCR/PDF text and never masquerades as the original scan.
4. **Recipe fields:** direct correction preserves order and target binding. Row operations remain keyboard reachable without presenting several large buttons per row.
5. **Issue/proposal:** exact field/step, source diagnosis, proposal value and uncertainty remain explicit. Accept/edit is a human review event; pending text never enters authoritative recipe data.
6. **Save draft:** records a durable draft and reports revision. It does not approve.
7. **Approve & next:** records approval for the exact effective revision and advances only after confirmation/readback. It does not import.

## Complexity and regression risk

The layout reduction is medium complexity. Narrow Source/Recipe switching adds focus, scroll, and widget-state risk because Streamlit reruns on interaction. Validate that partial edits, open proposals, page position, and the active queue candidate survive switching, resize, and browser re-entry. If native tabs reset the pane or focus, test an alternate native control before considering a component.
