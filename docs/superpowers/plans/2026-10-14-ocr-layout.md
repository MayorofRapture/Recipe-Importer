# OCR Layout Handling Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (recommended) or superpowers:subagent-driven-development to implement this plan task-by-task.

**Goal:** Preserve enough OCR geometry and source provenance to order columns, reject layout artifacts, and keep uncertain recipe fields reviewable.

**Architecture:** Extend the optional local OCR layer with lightweight positional line records. Render those records in logical reading order before the existing conservative recipe parser, and reuse the existing line-reference assembler for Gemini output. Introduce a new OCR/structuring cache version so old layout-free attempts remain readable but are not selected by the updated hybrid mode.

**Tech Stack:** Python 3.11+, optional Tesseract/pytesseract/Pillow, optional PyMuPDF, existing Pydantic models, unittest mocks.

## Constraints

- Keep raw OCR text unchanged and preserve source line ranges.
- Never silently repair corrupted quantities or instructions.
- Keep strict Gemini reference overlap validation unchanged.
- Keep OCR-derived candidates pending human review.
- Do not rewrite import history, approved review decisions, or existing caches.
- Tests must not require Tesseract, Gemini, Mealie, or production output.

### Task 1: Layout capture and logical order

- [ ] Add failing mocked image/PDF tests for bounding-box and positional line extraction.
- [ ] Implement optional Tesseract `image_to_data` capture and PyMuPDF positional text extraction.
- [ ] Order columns left-to-right and lines top-to-bottom while retaining page/source coordinates.
- [ ] Preserve legacy text-only fallback when optional layout methods are unavailable.

### Task 2: Conservative recipe layout parsing

- [ ] Add failing tests for website/page artifacts, multi-line titles, missing titles, corrupted fields, and numbered OCR wraps.
- [ ] Exclude only known generated/navigation/scan metadata from recipe fields and record uncertainty where content is questionable.
- [ ] Preserve paragraph/step boundaries, hyphenated line joins, and source line ranges.
- [ ] Keep every OCR candidate review-required.

### Task 3: Cache and workflow compatibility

- [ ] Add failing cache-version selection and approval/import idempotency tests.
- [ ] Version the layout OCR text and structured cache formats, retaining old files but selecting only the current version for `--ocr-structure`.
- [ ] Keep existing OCR-only, review, and normal import paths compatible.

### Task 4: Verification and documentation

- [ ] Add representative tests for the four reported source shapes.
- [ ] Update README with safe PowerShell reprocessing commands.
- [ ] Run the complete offline suite, compile check, CLI help, and diff check.
