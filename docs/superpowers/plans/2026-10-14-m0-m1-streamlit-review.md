# Recipe Importer M0/M1 Streamlit Review Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build a fixture-backed, durable Streamlit Review workspace that supports Quick Edit, Structured Edit, OCR warnings, and safe review decisions without uploads or live Mealie/Gemini calls.

**Architecture:** Keep existing extraction, identity, review, and import modules authoritative. Add thin read/projection adapters, a separate append-only draft store, pure Quick Edit conversion/validation helpers, and Streamlit pages that operate only through those adapters. M1 writes only drafts/review decisions under the configured local output directory; tests use temporary directories.

**Tech Stack:** Python 3, Pydantic, Streamlit 1.66, PyMuPDF, Pillow, `unittest`, Streamlit AppTest, Playwright CLI.

**Spec:** `docs/UI_UX_Specification.md`, `docs/UI_Implementation_Roadmap.md`, `docs/wireframes/recipe-importer-primary-workflows.md`

## Global Constraints

- No new uploads or live Gemini/Mealie calls in M0/M1.
- Existing extraction caches, review history, import state, and source files remain authoritative and are never rewritten by adapters.
- Drafts use a separate durable store and survive refresh/navigation/restart.
- Pending suggestions never enter authoritative recipe fields or an import-ready projection.
- Quick Edit is default: ingredients are one item per line; blank lines separate instruction steps.
- Approval must be durable and validated before navigation; failed save/approval never advances.
- Imported and Check Mealie recipes are protected from editing/approval.
- Preserve unrelated worktree changes and do not commit `.env` or `output/` contents.

## Review Focus

- Legacy cache shapes and missing source files must produce a usable read-only row, not crash the app. Test through the catalog adapter.
- Duplicate titles in one source must remain ambiguous/protected. Test stable identity and protected status.
- Quick Edit must preserve Unicode fractions, punctuation, suspicious lines, and blank-line boundaries. Test codec round trips.
- A failed durable draft write must retain the current in-session values and not approve or advance. Test injected store failure.
- Streamlit reruns must not duplicate review events. Test one approval interaction and inspect append-only event count.

### Task 1: M0 contracts, Quick Edit codec, and durable drafts

**Files:**
- Create: `ui_contracts.py`
- Create: `quick_edit.py`
- Create: `draft_store.py`
- Create: `tests/test_ui_contracts.py`
- Create: `tests/test_quick_edit.py`
- Create: `tests/test_draft_store.py`
- Modify: `requirements.txt` to make `PyMuPDF` and `Pillow` available to the UI

**Interfaces:**
- `quick_edit.parse_ingredients(text: str) -> ParseResult`
- `quick_edit.parse_instructions(text: str) -> ParseResult`
- `quick_edit.to_quick_edit(ingredients: list[str], instructions: list[str]) -> tuple[str, str]`
- `draft_store.DraftStore(path: Path).save(...) -> DraftRecord`
- `draft_store.DraftStore.latest(candidate_id: str) -> DraftRecord | None`
- `ui_contracts.normalize_status(...) -> str`

- [ ] Write failing tests for line parsing, warning detection, conversion preservation, draft restart/readback, atomic append behavior, and status normalization.
- [ ] Run focused tests and observe expected failures because the new modules do not exist.
- [ ] Implement the smallest pure codec and append-only draft store satisfying the tests.
- [ ] Add dependency floors needed for PyMuPDF/Pillow source previews.
- [ ] Run focused tests, then the existing offline suite.

### Task 2: M0 read adapters and sanitized fixtures

**Files:**
- Create: `ui_data.py`
- Create: `tests/ui_fixture_factory.py`
- Create: `tests/test_ui_data.py`
- Create: `tests/fixtures/ui/README.md`

**Interfaces:**
- `ui_data.load_catalog(output_dir: Path, source_root: Path | None = None) -> list[RecipeView]`
- `ui_data.render_source_page(path: Path, page_number: int) -> SourcePage`
- `ui_data.RecipeView` exposes stable ID, candidate, source/cache metadata, draft, warnings, suggestion decisions, normalized status, and protected flag.
- `ui_data.next_unresolved(views: list[RecipeView], current_id: str | None, filters: QueueFilters) -> RecipeView | None`

- [ ] Write failing adapter tests for legacy cache, versioned cache, review events, import state, reordered candidates, missing source preview, and protected imported records.
- [ ] Run focused tests and observe expected failures.
- [ ] Implement read-only cache/review/state projection using `candidate_identity`, `ReviewStore`, and existing quality helpers.
- [ ] Implement in-memory PyMuPDF/image preview with bounded rendering and graceful missing/unsupported fallback.
- [ ] Add sanitized fixture factory for Fried Rice, The Best Caramel Apples, Cranberry Date Bars, multi-page PDF metadata, imported, and Check Mealie cases.
- [ ] Run M0 adapter tests and full offline suite; confirm real `output/` mtimes/content are unchanged.

### Task 3: M1 Streamlit shell and Review page

**Files:**
- Create: `streamlit_app.py`
- Create: `app_pages/review.py`
- Create: `app_pages/upload.py`
- Create: `app_pages/import_history.py`
- Create: `app_pages/common.py`
- Create: `tests/test_streamlit_review.py`

**Interfaces:**
- `streamlit_app.py` registers Upload, Review, and Import & history with `st.navigation`.
- `app_pages/review.py` reads `ui_data.load_catalog` and writes only through `DraftStore`/`ReviewStore`.
- Review actions use stable session keys and preserve selected candidate/filter state.

- [ ] Write AppTest cases for default Review navigation, queue filtering/search, selecting a candidate, protected imported state, and coming-soon Upload/Import pages.
- [ ] Run AppTest and observe failures because the app/page modules do not exist.
- [ ] Implement the minimal navigation shell and Review queue with unresolved default.
- [ ] Implement PyMuPDF source preview, independent page/recipe navigation, Quick Edit default, Structured Edit, and safe mode switching.
- [ ] Implement Save draft, Save & Next, Skip for now, previous navigation, and end-of-queue states.
- [ ] Run AppTest and the full offline suite.

### Task 4: M1 warnings, suggestions, approval, and browser verification

**Files:**
- Modify: `app_pages/review.py`, `app_pages/common.py`, and relevant adapters
- Modify: `tests/test_streamlit_review.py`
- Create: `tests/test_review_workflows.py`

- [ ] Write failing workflow tests for warning display, pending suggestion exclusion, failed save recovery, approval readback, Approve & Next, no duplicate approval event, and mode-switch preservation.
- [ ] Run focused tests and observe expected failures.
- [ ] Implement field-level warning cards and separate suggestion dispositions.
- [ ] Implement validated durable approval and `Approve & Next`; never advance on failure.
- [ ] Add Playwright CLI smoke flow against a local Streamlit server, saving artifacts under `output/playwright/` only if the user later authorizes generated artifacts; use temp fixture output for the run.
- [ ] Inspect a rendered screenshot at desktop and narrow widths; fix only verified usability issues.
- [ ] Run AppTest, workflow tests, full offline suite, compileall, CLI help, and `git diff --check`.

### Task 5: M0/M1 documentation and handoff

**Files:**
- Modify: `README.md` with Windows PowerShell launch command and M1 limitation note
- Modify: `docs/UI_Implementation_Roadmap.md` with actual verification status

- [ ] Document `streamlit run streamlit_app.py` and fixture/test behavior without suggesting live import capability.
- [ ] Record Verified/Failed/Unverified/Blocked evidence and known limitations.
- [ ] Run final verification and inspect protected paths before committing.

