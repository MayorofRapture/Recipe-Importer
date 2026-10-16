# UI redesign wireframes and interaction index

**Proposed, synthetic content, not screenshots or implementation.** Date: 2026-10-14. These wireframes support [product experience](../../UI_Product_Experience.md) and [Luna roadmap](../../UI_Redesign_Roadmap.md). Current evidence lives in [the audit](../../UI_UX_Audit.md).

| File | Covers | Milestone |
| --- | --- | --- |
| [01 Uploads and extraction](01-uploads-extraction.md) | Global navigation, first run, upload, duplicates, processing, quota/recovery/completion | R4/R5 |
| [02 Review](02-review.md) | Queue, source/editor, row and bulk editing, proposals, save/approve/skip, protected/empty/error states | R1/R2/R5 |
| [03 Import and history](03-import-history.md) | Selection, preflight, separate creation/tagging, uncertainty, independent history | R3 |
| [Visual board](board.html) | Desktop review, bulk cleanup, upload/progress, import/history and narrow layout | R2–R4 concept review |

Open `board.html` locally in a browser; it uses embedded CSS and synthetic text only, with no network or scripts. Its links navigate between concept panels. It is a wireframe board, not a Streamlit prototype: fields/buttons illustrate placement and do not save/import. The Markdown contracts specify actual behaviors; a polished visual does not imply implementation feasibility or verified accessibility.

## Shared layout and state rules

1440×900 target: modest 24px margins, ~50px top navigation, ~44px context, ~72px recipe/action header; remaining viewport prioritizes source/editor. Queue is hidden unless requested. Default review panes ~45/55 with ~20px gutter. Exact pixel layout needs native Streamlit browser verification; do not create a brittle CSS replica of this board.

Primary actions: Uploads **Start extraction**; Review **Approve & next**; Ready import **Import n recipes**. Each is only enabled after its own prerequisites. Secondary Save/Skip/Previous are distinguishable. Import is never present in the review action strip.

Alternative layouts considered: a permanent queue/source/editor triple column, a source/editor tab interface and two comparison panes. Select two panes for desktop; drawer selection preserves comparison room, unlike the rail; narrow mode uses a labeled source/editor switch or single-page stack, unlike squeezing three columns. Detailed tradeoffs are in the product document.

Selection uses stable candidate ID; source pages use source digest and actual page count; editable rows use persistent UI row IDs bound to immutable extraction targets. No ordinary control displays full hashes.

Every frame must account for loading, no data, no filter matches, partial result, definite error, uncertainty and completion. No status is just a colored dot. Error copy says what happened, what remains safe, and the next action. Model/provenance details are secondary, but source limitations and consequential proposals are visible at the decision point.

## Scenario-to-frame coverage

| Scenario | Frames | Required evidence in implementation |
| --- | --- | --- |
| Fried Rice | Review default → saved → approved/next → import preflight | Small correction persists; fake payload exact |
| Caramel Apples | Bulk cleanup → Apply preview → restore/cancel | 15 artifact lines handled without 15 deletes |
| Cranberry & Caramel Date Bars | Critical issue/proposal → reject/defer → resume | No pending completion imported; source issue remains unresolved |
| Multi-page PDF | Page controls + queue → partial import → retry compare | Page/candidate navigation distinct; protected sibling untouched |
| Previously imported | Protected Review / History | No edit/approve/create enabled |
| Interrupted processing | Paused job / failed save / uncertain import | Durable state restored; no blind retry |

## Handoff consistency

R1 makes state semantics real before R2 presents them. R3 adds import to the existing library; R4 then provides new sources; R5 handles intentional re-extraction; R6 tests the whole journey. Disabled/placeholder future controls must say when unavailable in intermediate releases; never render a fake functional upload/import. No implementation begins before user approval of the design package.
