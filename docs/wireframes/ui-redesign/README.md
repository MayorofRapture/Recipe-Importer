# Recipe-Importer UI redesign evidence and wireframes

**The interaction wireframes remain proposals. UX-0 verification is complete; the Review layout redesign has not started.** Read the [current-state audit](../../UI_UX_Audit.md), [product experience](../../UI_Product_Experience.md), and [implementation roadmap](../../UI_Redesign_Roadmap.md) first.

## Current-state screenshot annotations

Four sanitized Review captures were made in isolated synthetic mode at device scale factor 1. The capture temporarily hides only the fixture attestation and local output path; no local filesystem metadata is present in the PNGs.

| Finding | What the current capture shows | Design response |
| --- | --- | --- |
| 1 | Save draft fits at 1440x900, but Approve & next is below the fold. Both fit at 1920x1080. | Keep decision actions near recipe identity without changing their authority. |
| 2 | Review title/status, queue navigation, Queue, and Options precede the work panes. | Compress the top stack to preserve room for source comparison. |
| 3 | At 768x900 source and editor remain side by side with narrow controls. | Switch to a labeled Source/Recipe view at a measured width. |
| 4 | At 320x900 the named menu opens to reveal destinations and keyboard reaches Review. The open menu squeezes and clips source/editor content; source comparison remains inefficient. | Keep navigation keyboard-operable and add a source/editor switch in D1. |
| 5 | Focus returns to the recipe title after Save draft and candidate navigation. | Preserve that handoff as the layout changes. |

Previously committed synthetic captures for Upload partial acceptance and Import/History are linked from the audit. They show the real local UI with fake services and must not be described as live Mealie evidence.

## Proposed wireframes

- [01 — Upload and extraction](01-uploads-extraction.md)
- [02 — Review, wide and narrow](02-review.md)
- [03 — Import and history](03-import-history.md)

The diagrams are placement/behavior proposals, not rendered prototypes. Native Streamlit controls are preferred; fixed positioning and custom components require a focused feasibility check.

## Evidence boundary

Automated Chromium checks verified the named recipe title/save/approve controls, keyboard access to Review at 320px, and focus restoration after save and candidate navigation. A full keyboard-only workflow, complete accessibility-tree audit, contrast sweep, 200% text resize, screen-reader validation, and live Gemini/Mealie tests were not completed. Current gaps are listed in the [audit](../../UI_UX_Audit.md).
