# M1.1 Review UI Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Redesign the existing Streamlit Review workflow into a compact, structured-editing workspace with top navigation, inline proposals, and fast recipe-to-recipe review without changing importer semantics.

**Architecture:** Keep `ui_data.py`, `draft_store.py`, `review_store.py`, and recipe-quality models as the authority. Refactor only the Streamlit shell/page composition and add small UI-facing helpers for structured edit buffers, inline suggestion placement, and top-level navigation state. Use native Streamlit widgets and theme configuration; do not add a frontend framework or live service calls.

**Tech Stack:** Python 3, Streamlit 1.66.0, AppTest, PyMuPDF, existing Pydantic review models, PowerShell-compatible test commands.

**Spec:** `C:\Users\Logan's PC\.codex\attachments\947875d6-c725-4911-873c-19adeab234ec\Pasted text.txt`

## Global Constraints

- Preserve existing extraction caches, review events, import state, source files, and draft persistence semantics.
- Pending suggestions remain outside authoritative recipe fields and Mealie payloads until an explicit suggestion decision is recorded.
- Imported and Check Mealie records remain protected and cannot be approved from the Review editor.
- No Gemini or Mealie calls; use sanitized temporary fixtures for UI tests.
- Use native Streamlit controls, Material Symbols/text labels, sentence-case copy, accessible labels, and `width="stretch"` instead of deprecated `use_container_width`.
- Keep primary actions visible, distinct, and safe: Save draft, Save & next, Approve & next, Previous, Skip.
- Review feedback must distinguish saved drafts, approval, and import readiness.

## Review Focus

- Reordering and deletion in structured ingredient/instruction buffers must not change unrelated entries; test stable field order and empty-row handling.
- Inline suggestion placement must preserve pending provenance and must not mutate the authoritative candidate before a decision; test pending, approved, edited, rejected, and keep-pending states.
- Switching Quick Edit and Structured Edit must preserve unsaved text in both directions; test suspicious OCR lines and blank-line instruction boundaries.
- Top navigation and queue filtering must retain the selected candidate and avoid rendering unrelated workflow bodies; test navigation state with AppTest.
- Long recipes and narrow viewports must remain usable without hiding primary actions; test layout tokens/classes and run rendered checks at desktop and narrow widths.

### Task 1: UI state helpers and structured editor contracts

**Files:**
- Create: `ui_review_state.py`
- Test: `tests/test_ui_review_state.py`

**Interfaces:**
- `StructuredBuffer` stores ordered ingredient and instruction strings plus source suggestion markers without changing `RecipeCandidate`.
- `structured_from_candidate(view: RecipeView, buffer: dict | None) -> StructuredBuffer` restores a draft or source candidate.
- `structured_to_quick_edit(buffer: StructuredBuffer) -> tuple[str, str]` and `quick_edit_to_structured(ingredients: str, instructions: str) -> StructuredBuffer` preserve existing codec rules.
- `apply_structured_change(buffer: StructuredBuffer, field: Literal["ingredients", "instructions"], index: int, value: str) -> StructuredBuffer` returns a copied buffer.
- `move_structured_item(buffer, field, index, direction) -> StructuredBuffer` and `remove_structured_item(buffer, field, index) -> StructuredBuffer` are bounds-safe.

- [ ] Write failing tests for restore, round-trip, add/remove/reorder, and suggestion markers.
- [ ] Run `& .\.venv\Scripts\python.exe -m unittest tests.test_ui_review_state`; expect missing-module failure.
- [ ] Implement the helpers with immutable copies and no writes to source/cache/review state.
- [ ] Re-run focused tests and then existing quick-edit/draft tests.
- [ ] Commit `feat: add structured review state helpers`.

### Task 2: Top navigation, queue bar, and compact visual system

**Files:**
- Modify: `streamlit_app.py`
- Modify: `app_pages/review.py`
- Modify: `app_pages/upload.py`
- Modify: `app_pages/import_history.py`
- Create: `.streamlit/config.toml` only if native theme settings are needed
- Test: `tests/test_streamlit_navigation.py`

**Interfaces:**
- The entrypoint exposes only one rendered workflow body at a time and provides top-level Upload, Review, and Import & history choices.
- Review page exposes a horizontal queue region with previous/next controls, searchable recipe selection, batch/status filters, title/status, and progress.
- Existing catalog loading, draft save, approval, and protected-record behavior remain unchanged.

- [ ] Write failing AppTest assertions for top workflow navigation, no sidebar review queue, queue progress, and selection persistence.
- [ ] Run the focused test and confirm it fails against sidebar navigation.
- [ ] Implement native top navigation/segmented controls and a compact queue bar; keep technical identifiers out of primary copy.
- [ ] Run focused navigation AppTests and the full existing suite.
- [ ] Commit `feat: move review navigation into compact top bars`.

### Task 3: Structured editor and inline suggestion rows

**Files:**
- Modify: `app_pages/common.py`
- Modify: `app_pages/review.py`
- Test: `tests/test_streamlit_structured_review.py`

**Interfaces:**
- Structured mode is the default for new candidates; Quick Edit remains available as a bulk-edit mode.
- Ingredients and instructions render as individually editable rows with add/remove/move controls and visible labels.
- Suggestions render beside their targeted row with Unverified suggestion, Accept, Edit, Reject, and Keep pending controls; decision writes use `ReviewStore` only.

- [ ] Write failing AppTest tests for structured default, row controls, inline suggestion state, and pending-suggestion approval blocking.
- [ ] Run focused tests and confirm the expected missing/default behavior.
- [ ] Implement structured buffer synchronization, inline suggestion placement, and decision feedback without embedding suggestion labels in saved text.
- [ ] Run focused tests plus review/import safety tests.
- [ ] Commit `feat: make structured review editing the default`.

### Task 4: Responsive action bar, accessibility states, and verification artifacts

**Files:**
- Modify: `app_pages/review.py`
- Modify: `streamlit_app.py`
- Test: `tests/test_streamlit_responsive_review.py`
- Create: `docs/UI_M1_1_Verification.md`

**Interfaces:**
- Action bar uses labeled Material Symbols/text buttons, emphasizes Approve & next, and never advances after a failed save or approval.
- Source preview has an accessible alternative and the editor remains usable at 1366x768, 1440x900, and narrow reflow widths.
- Verification document records AppTest, full suite, Playwright/screenshots if available, and accessibility scope/limitations.

- [ ] Write failing tests for disabled protected actions, error retention, action labels, and narrow-layout class/token output.
- [ ] Run focused tests and confirm failures before implementation.
- [ ] Implement compact action bar, responsive containers, focus-safe labels, and visible save/approval feedback.
- [ ] Run full tests, compileall, CLI help, Streamlit smoke test, Playwright happy/recovery flow if available, and accessibility audit against actual rendered evidence.
- [ ] Commit `feat: polish review workspace actions and responsive layout`.

## Completion Gate

- `python -m unittest discover -v` passes with no regressions.
- `python -m compileall -q .` passes for application/test modules.
- `git diff --check` passes for this branch's changes.
- AppTest covers navigation, structured editing, inline suggestion safety, persistence, protected records, and approval failure retention.
- Browser verification is reported as Verified or Unverified with the exact reason; no screenshot is presented as keyboard/DOM conformance proof.
- No production `output/` or `.env` changes are staged.
