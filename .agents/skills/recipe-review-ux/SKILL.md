---
name: recipe-review-ux
description: Use when designing, implementing, testing, or reviewing the Recipe-Importer Streamlit workflows for cookbook upload, extraction progress, recipe correction, approval, recovery, or Mealie import; keep the experience simple, safe, and understandable for repetitive personal recipe work.
---

# Recipe Review UX

## Purpose

Make complex recipe processing feel simple. Treat the application as an approachable personal recipe manager, not a developer console or technical administration dashboard. Design around complete user tasks, reduce context switching, and preserve the operator's work and trust.

This skill governs the whole Recipe-Importer application. It does not authorize changes to extraction, persistence, Gemini, Mealie, or import semantics; follow the root `AGENTS.md` and the existing pipeline contracts for those boundaries.

## Use with the other UI skills

- Use `developing-with-streamlit` for Streamlit layout, widgets, forms, session state, caching, theming, and performance.
- Use `product-design-and-ux` for task flows, information architecture, state/recovery models, and interaction contracts.
- Use `ui-ux-pro-max` for visual hierarchy, typography, spacing, component consistency, and design-system exploration. Its recommendations remain subordinate to Streamlit constraints and this skill's workflow requirements.
- Use `playwright` for real browser workflows, screenshots, and interaction evidence.
- Use `accessibility-audit` for screenshot, markup, or URL audits; do not call visual inspection a complete accessibility audit.

## Product principles

1. Prefer one clear next action over dense controls or technical terminology.
2. Keep navigation shallow: Uploads, Review, and Import/History should cover the main work.
3. Make saving, approving, and importing visibly different actions.
4. Keep source evidence, extracted recipe data, unverified suggestions, and approved edits visibly separate.
5. Never hide unsaved work, silently discard corrections, or imply that a pending suggestion is a fact.
6. Use progressive disclosure: ordinary users see the next step and plain-language explanation; diagnostics are available on demand.
7. Optimize repetitive work for keyboard use, stable focus, fast loading, and large batches.
8. Explain important design decisions in terms of reduced clicks, reduced context switching, lower error risk, or faster recovery.

## Required end-to-end workflows

### 1. Upload a cookbook or batch

- Accept drag-and-drop JPG, PNG, WEBP, and PDF files, plus a file-picker alternative.
- Support one recipe or a batch, and let the operator name or select a cookbook collection before extraction.
- Allow a cookbook tag to be chosen before import; do not silently retag historical recipes.
- Show a thumbnail or page preview, filename, page count when known, duplicate/previously-seen indication, and upload progress.
- Keep the upload surface useful when empty, loading, partially complete, or invalid. Explain accepted types and the next action.

### 2. Extract and recover

- Show batch progress in human terms: files waiting, recipes ready to review, recipes needing attention, and completed imports.
- Present success and failure per recipe, not only per source file; a multi-recipe file may have mixed outcomes.
- Explain quota exhaustion, transient failures, and OCR fallback without requiring the user to understand provider internals.
- Make retry target unresolved files/recipes only and preserve successful entries, earlier extraction versions, approvals, and corrections.
- Keep raw diagnostics behind an expandable details control with a plain-language summary and a suggested next action.

### 3. Review and correct a recipe

- Use a side-by-side source preview and structured recipe editor where the source remains visible while editing.
- Support multi-page sources and multiple recipes in one source with an explicit recipe selector and stable identity; never rely on array position alone.
- Make title, ingredients, instructions, yield, timing, temperature, and other supported metadata editable. Support adding, deleting, merging, and reordering entries.
- Highlight uncertain or unreadable content at the field or step that needs attention. Distinguish `missing from source`, `unreadable`, `ambiguous`, and `visible but not extracted`.
- Keep unverified completions in a separate proposal card showing the affected field, proposed value, rationale, uncertainty, and `pending` disposition. A pending proposal must not alter the authoritative recipe or import payload.
- Autosave drafts where practical and always show a visible save state. Preserve edits when moving between recipes, refreshing, retrying, or leaving and returning to a batch.
- Provide fast keyboard navigation through fields and recipes. Keep approve, reject, skip, and save actions reachable without hunting through menus.

### 4. Approve and import into Mealie

- Use a clear review gate: only a complete authoritative recipe or an explicitly human-approved/edited value can become importable.
- Distinguish `Save draft`, `Approve`, and `Import`; approval is not an import and import is not a save.
- Support controlled single-recipe and batch imports with a visible count of selected approved recipes.
- Show duplicate prevention and import history without promising success before the server outcome is known.
- Show recipe creation and batch-tagging outcomes separately. Preserve existing tags and make backfill/retag an explicit scoped operation.
- For an ambiguous response, use plain language such as “Check whether Mealie received this recipe” and do not offer blind retry as the default.

### 5. Resume and maintain

- Make interrupted batches resumable from the last durable state.
- Provide a focused “Needs attention” view with source, recipe title, plain-language diagnosis, and one useful next action.
- Preserve prior approvals, corrections, extraction versions, and import history while retrying.
- Keep technical diagnostics inspectable when needed, but out of the primary task path.

## State and content rules

Use user-facing labels that describe what the operator can do next. Never expose raw internal values such as `OCR_STRUCTURED_NEEDS_REVIEW` in the primary UI.

| User-facing state | Meaning | Primary action |
| --- | --- | --- |
| Waiting | Uploaded and queued | Start extraction |
| Extracting | Work is in progress | View progress |
| Ready to review | A recipe is available for checking | Review |
| Needs attention | A critical detail is missing, unreadable, ambiguous, or not extracted | Fix, approve/edit a proposal, or skip |
| Ready to import | The authoritative recipe passed review | Import |
| Imported | Mealie creation is confirmed | View history |
| Import failed | The server gave a definite failure | Inspect and retry intentionally |
| Check Mealie | The server outcome is uncertain | Reconcile before retrying |

Always make empty, loading, error, partial-success, and success states actionable. State what happened, what is safe, and the next available action.

## UX review gate

Before calling a UI milestone ready, read [references/ux-review-checklist.md](references/ux-review-checklist.md) and verify the representative scenarios. Review an actual rendered screen with Playwright and inspect a screenshot; use the accessibility skill for an accessibility verdict. A unit test or static code review alone is not a usability release check.

For each important design decision, record one short reason tied to user effort or safety, for example: “Keep the source preview pinned while editing so the operator does not switch tabs to verify a quantity.”

## Non-goals

- Do not build the Streamlit app, alter the importer, or introduce a second source of truth from this skill alone.
- Do not rank recipes, choose a “best” extraction, or auto-apply an AI suggestion.
- Do not hide uncertainty behind polished copy or replace a durable state transition with a visual-only status.
