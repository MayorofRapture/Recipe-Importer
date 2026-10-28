# D2 — Contextual issues and proposals

**Status:** Implementation complete; awaiting independent review.
**Date:** 2026-10-27
**Scope:** Put source diagnoses and pending proposals beside the exact recipe fields they concern. Preserve D1 layout and all existing review decisions.

## Interaction

- Critical source diagnoses appear beside the title, ingredient, instruction, or optional field they describe. The summary names the source condition; evidence, explanation, and next action stay in an expander.
- Row diagnoses follow the registered source target ID, so moving an ingredient or instruction does not move its issue to a different row.
- Optional metadata is labeled `Optional note`; it does not read as a critical blocker.
- Row and section proposals now have a short expander label with the proposed text and an explicit `Unverified` marker. Existing comparison, accept, edit, reject, rebase, and decision handling remains inside the expanded proposal card and continues through the existing review services.
- The general Issues view remains available for full review and resolution. No diagnosis or proposal decision is inferred from editing the recipe field.

These summaries reduce scrolling while retaining the source context. Expandable details keep evidence close when needed and leave more room for the recipe scan.

## Evidence

All images use the attested synthetic browser fixture.

- [1440px contextual diagnoses](evidence/d2-contextual-issues-1440px.png)
- [768px contextual diagnoses](evidence/d2-contextual-issues-768px.png)
- [320px contextual diagnoses](evidence/d2-contextual-issues-320px.png)
- [Expanded unverified proposal](evidence/d2-proposal-details-1440x900.png)

The D2 Chromium check confirmed the title missing-from-source summary, ambiguous and unreadable ingredient summaries, optional yield note, and pending unverified proposal at 1440px, 768px, and 320px. There was no horizontal overflow. Expanding the proposal exposed its rationale, uncertainty, comparison, and existing decision controls. The isolated review event ledger contained no new revision, approval, or issue-resolution event from this presentation check.

AppTest covers all four required diagnoses, including visible-but-not-extracted on the Instructions tab, confirms evidence details are available, and verifies that pending proposal text remains separate from the authoritative ingredient value. The full offline suite passed **617 tests, 2 skipped**.

The existing synthetic Chromium journey passed its proposal reject/edit/accept path, critical issue retention, approval, and fake import. Separate D1 responsive checks passed at 1920x1080, 1440x900, 768x900, and 320x900, including Instructions tab, unsaved edit, source page, and zoom retention; 200% root text emulation had no horizontal overflow.

## Limits

- No Gemini or Mealie service was contacted. The import journey used the local fake service.
- Browser screenshots and accessibility-tree inspection do not establish screen-reader behavior or full WCAG conformance.
- D2 changes presentation only. Approval and import eligibility continue to use the existing authoritative review projection and backend checks.

Stop after D2 for independent review. D3 and D4 remain outside this implementation.
