# Hybrid OCR-to-JSON Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Convert preserved local OCR text into reviewable `RecipeCandidate` JSON deterministically when possible, using text-only Gemini line references only for ambiguous structure, without introducing a second import architecture.

**Architecture:** Extend the existing `local_ocr.py` parser with explicit source-line structure and provenance. Add a small line-reference response/assembly boundary in `import_recipes.py` that uses the existing `google_client`, `ModelRouter`, quota ledger, cache, review store, and `process_file` import safety. Add `--ocr-structure` as an extraction-only orchestration path that never connects to Mealie, while saving a compatible structured cache for a later explicit import run.

**Tech Stack:** Python 3.11+, Pydantic, existing `google-genai` client, `ModelRouter`, `ReviewStore`, `RecipeCandidate`, unittest mocks, and optional PyMuPDF/Tesseract OCR dependencies.

**Spec:** User-provided `Hybrid OCR-to-JSON Pipeline` pasted specification in this conversation.

## Global Constraints

- Preserve raw OCR text exactly; only structured fields may be normalized for existing schema compatibility.
- Gemini text-only structuring may return one-based inclusive source line references, never rewritten recipe content.
- Invalid or overlapping references are rejected or held for review; never guess.
- All OCR-derived candidates require explicit human review before Mealie import.
- Preserve existing caches, review decisions, import history, identities, and duplicate prevention.
- Tests use mocked Gemini and Mealie; no live API calls or production output writes.

## Review Focus

- Timestamp/header before a title: parser must select the recipe title and preserve the timestamp in raw OCR.
- Numeric recipe titles: metadata filtering must not discard legitimate titles containing numbers.
- Paragraph and numbered instructions: assembly must preserve complete source lines and ordering.
- Multiple/noncontiguous spans: each recipe must receive only its referenced source sections.
- Invalid/overlapping references and RECITATION: raw text remains saved and the result remains reviewable.

### Task 1: Deterministic OCR structuring

**Files:**
- Modify: `local_ocr.py`
- Modify: `recipe_quality.py` only if provenance fields are needed, backward-compatible defaults only
- Test: `tests/test_local_ocr.py`

- [ ] Add failing tests for Fried Rice, common heading variants, numbered/paragraph instructions, multiple recipes, and exact raw-text preservation.
- [ ] Implement deterministic parsing with a result that distinguishes complete structure from ambiguous structure and records source-line spans/method metadata.
- [ ] Run focused OCR tests and confirm the red-green cycle.

### Task 2: Text-only Gemini line-reference structuring

**Files:**
- Modify: `import_recipes.py` or create one focused `ocr_structuring.py` module if circular imports would otherwise result
- Modify: `model_routing.py` only for existing route-context/error reuse
- Test: `tests/test_ocr_structure.py`

- [ ] Add mocked tests for numbered prompts, valid multi-recipe/noncontiguous references, invalid/out-of-bounds references, overlap detection, invalid JSON, and RECITATION.
- [ ] Implement a constrained line-reference response model and text-only request using the existing model router/quota accounting.
- [ ] Validate one-based inclusive ranges and reject invalid/overlapping spans without reconstructing content through Gemini.
- [ ] Assemble candidates locally from the original OCR lines and attach provenance/confidence/uncertainty plus critical review findings.

### Task 3: `--ocr-structure` orchestration and cache/review integration

**Files:**
- Modify: `import_recipes.py`
- Modify: `review_store.py` only for a backward-compatible candidate-level approval/edit path if required by existing workflow
- Test: `tests/test_ocr_structure.py`, `tests/test_review_import.py`

- [ ] Add CLI tests proving `--source`, `--limit`, `--workers`, no Mealie calls, and no Gemini call for deterministic OCR.
- [ ] Add the opt-in branch that reuses digest-named OCR text, parses locally first, calls text-only Gemini only when ambiguous, saves a versioned structured cache, and writes review/report rows.
- [ ] Ensure approved/edit decisions are applied by the existing `process_file` path and existing imported identities are not recreated.
- [ ] Make repeated processing reuse OCR/structured cache and avoid duplicate review events.

### Task 4: Reporting, documentation, and verification

**Files:**
- Modify: `README.md`
- Modify: `.env.example` only if a new setting is required
- Test: existing full suite plus focused report tests

- [ ] Report OCR engine/status, structuring method, model if used, recipe count/title, confidence/uncertainty, cache path, and failure reason.
- [ ] Document PowerShell commands for the Fried Rice OCR input and remaining failed-source workflow.
- [ ] Run the complete unittest suite with isolated temp directories, compileall, CLI help, and `git diff --check`.
