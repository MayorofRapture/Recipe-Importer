# 01 — Upload and extraction

**Status: proposal.** Keep the current batch, staging, job, consent, and range services. Reorder presentation only after approval.

## Returning-user first view

```text
Upload
Current batch: Sunday recipes · 27 files · 4 ready · 2 need attention
[Add scans] [Resume extraction] [Open Review]

Files in this batch
✓ page-01.jpg · 1 page · Ready to review
… chapter-4.pdf · 18 pages · Reading selected ranges
! page-19.png · Could not read file · Choose another file

Extraction and cookbook ranges ▾
```

## First-use view

```text
Upload recipes
Create a named batch to keep scans and progress together.
[Batch name________________] [Optional Mealie tag____________]
[Create batch]

Add PDF, JPG, PNG, or WEBP scans
[Choose files]  Files are saved independently; a rejected file does not discard others.
```

After creating/selecting a batch, show the source list and per-file accepted/rejected outcomes first. Keep each saved source and its preview available. Put extraction mode and cloud consent immediately before Start extraction. Show pause/resume and per-source/per-recipe status in the job area.

Cookbook range selection stays a separate disclosure: original PDF, inclusive page bounds, overlap confirmation, selected range IDs, extraction method/consent, progress, and resume status. Keep the original immutable. A completed range can enter Review; the UI must continue to say that range import is disabled.

## Responsive and error states

- At narrow widths, batch selection, file outcomes, extraction controls, and progress stack in that order; keep file status and the safe next action visible without clipping.
- Error copy identifies the specific file/action and what remains saved. A partial upload keeps valid siblings selectable.
- A quota pause says work is paused and which unfinished ranges remain; it must not say the batch is complete.
- Avoid raw source paths and full hashes in the normal page. Keep those under optional diagnostics.

## User value and risk

Putting recent work first reduces time spent reopening a batch among hundreds of recipes. Separating ordinary sources from cookbook ranges prevents users from confusing their different processing controls. Regression risk is medium if controls are hidden: retain consent, duplicate/source integrity, pause/resume, partial-success and range-import gates in both layout and tests.
