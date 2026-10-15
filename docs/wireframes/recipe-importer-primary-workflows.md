# Recipe-Importer Primary Workflow Wireframes v2

These are implementation-neutral wireframes. They specify hierarchy, states, and actions; they are not Streamlit code or pixel-perfect mockups.

## Global shell

```text
┌──────────────────────────────────────────────────────────────────────────────┐
│ Recipe Importer                    Upload     Review (4)     Import & history │
│                                  7 ready · 4 need attention · 1 check Mealie │
└──────────────────────────────────────────────────────────────────────────────┘
```

Keep navigation limited to these three destinations. Counts are text and links, not color-only indicators.

## Primary workflow

```text
┌─────────────┐    ┌─────────────┐    ┌──────────────────┐    ┌──────────────┐
│ Open Review │ -> │ Compare scan│ -> │ Quick Edit       │ -> │ Approve &    │
│ queue       │    │ and recipe  │    │ or Structured    │    │ Next         │
└─────────────┘    └─────────────┘    │ Edit, save       │    └──────┬───────┘
                                      └──────────────────┘           │
                                                                     v
                                                              next unresolved
                                                              or end-of-queue
```

Approval is not import. After the queue is complete, the operator intentionally opens Import & history.

## Review — desktop, Quick Edit default

```text
┌─────────────────────────────────────────────────────────────────────────────────────────────┐
│ Review recipes          Search [________________]  [Needs attention ▾]  [Unresolved only ✓]  │
├─────────────────────┬─────────────────────────────┬───────────────────────────────────────────┤
│ QUEUE               │ SOURCE                      │ RECIPE                                    │
│ 4 need attention    │ cookbook_pages.pdf          │ The Best Caramel Apples [Needs attention] │
│ 7 ready to import   │ Page [<] 2 of 4 [>]         │ 15 lines to check · Draft saved          │
│                     │ Recipe 2 of 3               │                                           │
│ > The Best          │ ┌─────────────────────────┐ │ [Quick Edit] [Structured Edit]           │
│   Caramel Apples    │ │                         │ │                                           │
│   2 warnings        │ │   PyMuPDF page preview │ │ Ingredients — one item per line         │
│                     │ │                         │ │ ┌─────────────────────────────────────┐ │
│   Fried Rice        │ │   source remains       │ │ │ 2 cups apples                       │ │
│   Ready to import   │ │   visible while edit   │ │ │ 1 cup caramel                       │ │
│                     │ │                         │ │ │ %                                   │ │
│   Cranberry Date    │ └─────────────────────────┘ │ │ Noy 0 9 2021                       │ │
│   Bars · 1 warning  │ Evidence: page 2, ingredients│ │                                     │ │
│                     │ [View original OCR text]     │ └─────────────────────────────────────┘ │
│   Old Cookbook      │ [View extraction details]    │ 4 entries · 2 warnings                  │
│   Imported          │                              │                                           │
│                     │                              │ Instructions — blank lines separate steps│
│                     │                              │ ┌─────────────────────────────────────┐ │
│                     │                              │ │ Mix ingredients.                     │ │
│ [Show all]          │                              │ │                                     │ │
│                     │                              │ │ Bake until done.                     │ │
│                     │                              │ └─────────────────────────────────────┘ │
│                     │                              │ 2 structured steps                     │
│                     │                              │                                           │
│                     │                              │ CHECK THIS TEXT                         │
│                     │                              │ Line looks like OCR noise. Original kept.│
│                     │                              │ [Edit line] [View source] [Leave later] │
│                     │                              │                                           │
│                     │                              │ RESULT TO SAVE                           │
│                     │                              │ 2 ingredients · 2 steps · 1 warning     │
│                     │                              │                                           │
│                     │                              │ [Save draft] [Save & Next]               │
│                     │                              │ [Approve & Next] [Skip for now]          │
└─────────────────────┴─────────────────────────────┴───────────────────────────────────────────┘
```

The editor makes multiline cleanup the default. The source preview and original OCR remain available beside it.

## Review — Structured Edit mode

```text
┌─────────────────────────────────────────────────────────────────────┐
│ The Best Caramel Apples                         [Quick Edit] [Structured Edit ✓] │
│                                                                     │
│ Ingredients                                                        │
│ 1 [2 cups apples________________________] [↑] [↓] [Delete]         │
│ 2 [1 cup caramel________________________] [↑] [↓] [Delete]         │
│ 3 [_____________________________________] [↑] [↓] [Delete]         │
│ [+ Add ingredient]                                                   │
│                                                                     │
│ Instructions                                                        │
│ 1 [Mix ingredients._____________________] [↑] [↓] [Delete]         │
│ 2 [Bake until done._____________________] [↑] [↓] [Delete]         │
│ [+ Add instruction]                                                  │
│                                                                     │
│ Quick Edit preview: 2 ingredients · 2 steps                         │
│ [Show conversion warnings]                                           │
│                                                                     │
│ [Save draft] [Save & Next] [Approve & Next] [Skip for now]          │
└─────────────────────────────────────────────────────────────────────┘
```

Moving between modes captures current unsaved values first. If a conversion could merge lines or create suspicious fragments, show a preview and require **Switch and keep preview** or **Stay in current mode**. No text is silently discarded.

## OCR warning and unverified suggestion cards

```text
┌──────────────────────────────────────────────────────────────────────┐
│ CHECK THIS TEXT · Ingredient 3                                       │
│ “%” looks like OCR noise rather than a complete ingredient.          │
│ Original text is preserved. Correct it against the source or leave it│
│ unresolved.                                                          │
│ [Edit ingredient 3] [View source] [Leave for later]                  │
└──────────────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────────────┐
│ NOT SHOWN IN THE SOURCE · Critical                                  │
│ The scan does not print a quantity for this ingredient.              │
│ Unverified suggestion: 2 tablespoons                                 │
│ Rationale: plausible, but not confirmed by this source.              │
│ [Approve suggestion] [Edit value] [Reject] [Keep pending]            │
└──────────────────────────────────────────────────────────────────────┘
```

The first card describes possible extraction corruption. The second describes an omission in the original source. Neither automatically changes the recipe.

## Save failure and unsaved navigation

```text
┌──────────────────────────────────────────────────────────────────────┐
│ We could not save this draft.                                        │
│ Your edits are still open in this session. Nothing was approved.     │
│ [Retry save] [Download recovery text] [Stay on recipe]               │
└──────────────────────────────────────────────────────────────────────┘
```

When navigating with unsaved changes:

```text
Save your changes before leaving?
[Save and continue] [Leave without saving] [Stay here]
```

The page never advances after a failed save or approval.

## Approve & Next success and end of queue

```text
┌──────────────────────────────────────────────────────────────────────┐
│ Approved and saved                                                    │
│ The Best Caramel Apples is ready to import.                           │
│ Opening the next unresolved recipe…                                  │
└──────────────────────────────────────────────────────────────────────┘
```

At the end:

```text
┌──────────────────────────────────────────────────────────────────────┐
│ You have reached the end of this review queue.                        │
│ 4 recipes are ready to import · 0 need attention                      │
│ [Open Import & history] [Review all recipes] [Stay here]              │
└──────────────────────────────────────────────────────────────────────┘
```

Do not wrap silently to the first recipe.

## Narrow-screen Review

```text
┌──────────────────────────────┐
│ Review (4)                   │
│ [Search recipes____________] │
│ [Needs attention ▾]          │
├──────────────────────────────┤
│ The Best Caramel Apples      │
│ Needs attention · 2 warnings │
│ [Open recipe]                │
├──────────────────────────────┤
│ The Best Caramel Apples      │
│ [View source page 2 of 4]    │
│ [Quick Edit] [Structured]    │
│                              │
│ Ingredients                  │
│ [multiline text area_______] │
│                              │
│ Instructions                 │
│ [multiline text area_______] │
│                              │
│ 2 warnings · source kept     │
│ [View warnings]              │
│                              │
│ [Save draft]                 │
│ [Save & Next]                │
│ [Approve & Next]             │
│ [Skip for now]               │
└──────────────────────────────┘
```

The source is a labeled disclosure rather than a permanently visible second column. Action buttons remain in normal flow; a sticky action bar is allowed only if focus is never obscured.

## Import & history

```text
┌──────────────────────────────────────────────────────────────────────────────┐
│ Import & history                                                             │
│ Ready to import: 3 · Excluded: 2 need attention · 1 pending suggestion       │
│ Batch tag: October 2026                                                       │
│                                                                              │
│ READY TO IMPORT                                                             │
│ Fried Rice              Approved       [Include]                              │
│ Apple Cake              Approved       [Include]                              │
│                                                                              │
│ [Import approved recipes]                                                     │
│ This creates 2 recipes. Pending suggestions are excluded.                    │
│                                                                              │
│ HISTORY / NEXT ACTION                                                        │
│ Fried Rice       Recipe: Imported       Tag: Applied             Done         │
│ Apple Cake       Recipe: Imported       Tag: Needs attention      [Repair tag]│
│ Cranberry Bars   Recipe: Check Mealie   Tag: Not attempted        [Reconcile] │
│ Old Recipe       Recipe: Import failed  Tag: Not attempted        [Review]    │
└──────────────────────────────────────────────────────────────────────────────┘
```

Tag repair never recreates a recipe. Check Mealie opens reconciliation and does not offer blind retry as the default.

## Future Upload workflow

```text
┌──────────────────────────────────────────────────────────────────────────────┐
│ Upload recipe scans                                                         │
│ Add individual scans or a cookbook batch. Review everything before import.  │
│                                                                              │
│ ┌────────────────────────────────────────────────────────────────────────┐ │
│ │ Drop JPG, PNG, WEBP, or PDF files here     [Choose files]              │ │
│ └────────────────────────────────────────────────────────────────────────┘ │
│                                                                              │
│ Cookbook/group [ October cookbook________________ ]                         │
│ Batch tag      [ October 2026___________________ ] optional                │
│                                                                              │
│ fried-rice.jpg       1 page     Ready       [Remove]                       │
│ cookbook.pdf         14 pages   Previously seen — review existing result   │
│                                                                              │
│ [Start or resume processing]                                                │
└──────────────────────────────────────────────────────────────────────────────┘
```

Progress is durable and independent of an open browser:

```text
Reading 3 of 12 files · found 7 recipes
4 ready to review · 2 need attention · 1 waiting
[Open Review] [Retry unresolved work later]
```

