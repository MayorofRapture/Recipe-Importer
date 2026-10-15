# OCR Reading-Order Regression Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (recommended) or superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Preserve the legacy OCR stream, select layout reconstruction only when it is demonstrably safer, and flag spatially ambiguous recipe text instead of silently reordering it.

**Architecture:** Capture `image_to_string` and bounding-box `image_to_data` independently. Build layout text from coordinate-defined regions/columns, score both candidate streams for recipe structure and ordering, and choose the safer stream while retaining both raw files and provenance in the result/report. Keep existing review, cache, and import identities unchanged, with a new OCR layout version for reprocessing.

**Tech Stack:** Python, optional Pillow/pytesseract/PyMuPDF, existing Pydantic recipe models, `unittest`.

**Spec:** User request in the current conversation.

## Global Constraints

- Preserve original OCR text byte-for-byte alongside layout-aware output.
- Never silently reinterpret corrupted quantities or invent recipe content.
- Preserve strict Gemini reference-overlap validation, review approvals, import state, and duplicate prevention.
- Do not call live Gemini or Mealie services during tests.
- Do not overwrite existing production OCR, extraction, review, or import records.

## Review Focus

- A one-column recipe with scattered rotated artifacts must keep the original step order.
- A genuine two-column recipe must group ingredient and instruction regions without vertical interleaving.
- Numbered steps must remain monotonic or be held for manual review.
- Bad titles, placeholders, mixed-field lines, and corrupted quantities must lower confidence.
- Older cache/review/import records must remain readable and untouched while a new OCR version is generated.

### Task 1: Capture and compare both OCR streams

**Files:**
- Modify: `local_ocr.py`
- Modify: `import_recipes.py`
- Test: `tests/test_local_ocr.py`, `tests/test_ocr_structure.py`

- [ ] Add failing tests asserting original and layout text are both retained, the safer stream is selected for Caramel Apples, and reports expose both paths/selection reason.
- [ ] Run the focused tests and verify they fail for the missing dual-stream behavior.
- [ ] Add result metadata and separate versioned paths for original, layout, and selected OCR text; do not alter old OCR-only cache paths.
- [ ] Run focused tests and verify they pass.

### Task 2: Replace global vertical ordering with coordinate regions

**Files:**
- Modify: `local_ocr.py`
- Test: `tests/test_local_ocr.py`

- [ ] Add failing synthetic bounding-box tests for a left ingredients/right instructions page and a one-column page with unrelated right-side artifacts.
- [ ] Run the focused tests and verify the current largest-gap/block ordering fails.
- [ ] Implement coordinate-based region grouping: detect stable columns from overlapping x-ranges, keep full-width headers separate, order each region top-to-bottom, and never merge unrelated blocks solely by y-position.
- [ ] Preserve page/box/source metadata for every emitted line.
- [ ] Run focused tests and verify they pass.

### Task 3: Add quality scoring and conservative review diagnostics

**Files:**
- Modify: `local_ocr.py`, `recipe_quality.py`
- Test: `tests/test_local_ocr.py`

- [ ] Add failing tests for out-of-order numbered steps, mixed ingredient/instruction lines, implausible/placeholder titles, fragmented text, and `%`/`2%` quantities.
- [ ] Implement diagnostics and stream comparison without changing source text; lower confidence and require review whenever layout quality is uncertain.
- [ ] Ensure numeric recipe titles remain valid when they pass the title plausibility checks.
- [ ] Run focused tests and verify they pass.

### Task 4: Cache/report compatibility and documentation

**Files:**
- Modify: `import_recipes.py`, `README.md`
- Test: `tests/test_ocr_structure.py`, `tests/test_ocr_only.py`

- [ ] Add failing compatibility tests for versioned dual OCR paths, retained prior caches, idempotent reruns, and unchanged review/import identity.
- [ ] Version only the new layout/selection artifacts and keep legacy paths readable; add report fields for original/layout/selected OCR and quality reason.
- [ ] Document safe two-source regeneration and side-by-side inspection commands.
- [ ] Run the complete suite, compile check, CLI help check, and `git diff --check`.

