# RECITATION local OCR fallback

## Goal

Stop Gemini model rotation for `finish_reason=RECITATION`, preserve the response diagnostics, and recover readable source text locally into the existing manual-review path without changing successful Gemini imports or production state.

## Test-first implementation

1. Add mocked tests for RECITATION classification and router short-circuiting.
2. Add isolated local OCR tests for images, selectable PDFs, scanned PDFs, missing Tesseract, and marker-separated multi-recipe text.
3. Add an integration regression using temporary state/cache/report paths to verify OCR recovery becomes manual review and does not create a duplicate Mealie import.
4. Implement the smallest local OCR module with optional imports and injectable test seams.
5. Connect RECITATION failures to OCR, raw-text persistence, report fields, and the existing review ledger.
6. Update optional OCR dependencies, Windows configuration, README instructions, and verification commands.

## Safety boundaries

- Never call Gemini, Mealie, or Tesseract in tests.
- Never write to the repository's existing `output/` data during tests.
- Never write OCR output into the Gemini extraction cache or replace import history.
- OCR candidates always carry a critical manual-review diagnostic and cannot be imported automatically.
- Preserve the original Gemini finish reason, response metadata, attempted model, and route in the diagnostic log.

## Verification

- Run the complete unittest suite with an isolated workspace temp directory.
- Run compileall, CLI help, and `git diff --check`.
- Confirm no live API calls and no changes to existing production output files.
