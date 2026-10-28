# D2 Contextual Review Issues Implementation Plan

> **For agentic workers:** Use the executing-plans skill inline. Keep the work inside D2.

**Goal:** Show concise, correctly targeted source issue and proposal summaries beside the affected recipe fields while retaining expandable detail and existing review authority.

**Architecture:** Keep issue and proposal data sourced from the current review projection and draft buffer. Add presentation helpers in `app_pages/review.py`; all decisions continue through existing resolution and suggestion services. Extend AppTests and the synthetic Chromium journey for pending, missing, ambiguous, and visible-but-not-extracted cases.

**Tech Stack:** Python, Streamlit 1.66, unittest, Streamlit AppTest, Playwright/Chromium.

**Spec:** `docs/UI_Redesign_Roadmap.md` D2; `docs/wireframes/ui-redesign/02-review.md`; `docs/UI_UX_Specification.md` section 6.

## Global Constraints

- Preserve ReviewStore, DraftStore, candidate identity, source provenance, approval rules, import projection, and Mealie behavior.
- Pending proposal values remain visibly unverified and separate from authoritative recipe text.
- Keep all decisions in existing review services; do not change diagnosis, identity, or approval semantics.
- Use synthetic fixtures and isolated output only; no live Gemini or Mealie calls.
- Preserve `.vscode/`, `graphify-out/`, caches, histories, and source scans.
- Keep D1 pane switching, editor state, responsive layout, and focus behavior intact.

## Review Focus

- A field-level finding maps only to its exact field/row and does not drift after row changes.
- Missing, unreadable, ambiguous, and visible-but-not-extracted findings remain distinct and actionable.
- Pending proposals appear unverified, never merge visually with authoritative field text, and preserve their existing decision actions.
- Optional metadata does not appear as a critical blocker.
- Details remain available in the full issue view, and compact contextual summaries fit the 320px layout.

---

### Task 1: Contextual issue and proposal summaries

**Files:**
- Modify: `app_pages/review.py`
- Test: `tests/test_streamlit_structured_review.py`, `tests/test_streamlit_review.py`
- Test: existing synthetic Chromium Review journey and D2 browser fixture checks

**Interfaces:** Keep the current view, finding, suggestion, buffer, and service interfaces. New helpers are presentation-only and derive exact field/row matches from existing target bindings.

- [x] Add AppTests proving summaries are adjacent to the exact affected title/ingredient/step, include diagnosis and expandable details, and do not surface optional metadata as a critical issue.
- [x] Add AppTests proving pending proposals are labeled unverified and the authoritative field value is unchanged until the existing explicit action is selected.
- [x] Run each focused test and confirm it fails for the missing contextual summary before implementation.
- [x] Implement concise native Streamlit summaries with expandable evidence/rationale; keep full resolution controls in the existing Issues section if duplicating them inline would alter authority or overload the editor.
- [x] Run focused AppTests and the full offline suite; record exact results.
- [x] Run the synthetic Chromium journey and D2 fixture at 1440px, 768px, and 320px; verify issue text, proposal labeling, no overflow, existing editor state, and action reachability.
- [x] Run Python compilation, credential-free CLI help, JavaScript syntax checks, and `git diff --check`.
- [x] Review the diff for UI-only scope, then stop for independent review; do not begin D3 or merge PR #3.
