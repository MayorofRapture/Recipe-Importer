# Mealie compatibility and large cookbook PDF capability

## F8: empty instructions

### Current evidence

- The repository does not identify the exact installed Mealie release or provide a versioned OpenAPI response for the user's server.
- `.env.example` keeps `MEALIE_SUPPORTS_EMPTY_INSTRUCTIONS=false` by default.
- Import preflight blocks an empty-instructions recipe unless that explicit local setting is enabled.
- The offline client contract test verifies that the current Mealie v3 adapter sends `recipeInstructions: []` unchanged to `/api/recipes/create/html-or-json` and does not synthesize a step.
- Mock import tests cover the current wrapper accepting the request, a server rejection, and preventing an uncertain/rejected attempt from being posted repeatedly.

These tests establish the outgoing request and importer safeguards. They do **not** establish how any installed Mealie version stores the recipe, whether it adds a default step, or what a subsequent GET returns. Live compatibility is **Blocked** pending version-matched verification and explicit authorization for one disposable Mealie write. The global gate remains disabled.

### Safe acceptance procedure when authorized

1. Record the exact Mealie version and confirm the test target is a disposable instance or a separately backed-up staging database running the same release and configuration as the intended server.
2. Confirm no recipe with the test slug or unique test title already exists. Use one disposable seasoning recipe with a unique title, one ingredient, and an intentionally empty instruction list.
3. Keep `MEALIE_SUPPORTS_EMPTY_INSTRUCTIONS=false` during setup. Authorize exactly one create request against the disposable target; do not run the whole import batch.
4. Inspect the captured request body and confirm `recipeInstructions` is an empty list. Inspect the create response, then GET that exact returned slug and verify the persisted instruction list is still empty and no localized/default step was inserted.
5. Record the Mealie version, request/response shape, persisted result, and cleanup result without retaining credentials or private recipe content. Delete only the uniquely identified disposable recipe after explicit cleanup authorization.
6. Enable the setting only for that verified installation, then restart the app and run the single-recipe preflight. If the response is ambiguous, the recipe is protected for reconciliation; do not repeat the create.

Without the exact server version and authorization in step 3, leave the gate disabled and the item blocked.

## F9: whole-book PDFs and page ranges

### Current behavior and limit

- `BatchStore` defaults to a 50 MiB per-file limit and a 100-page PDF limit. It rejects a 101-page PDF before staging it.
- An accepted PDF is staged and hashed as one immutable source. The upload preview renders only its first page.
- Local PDF OCR iterates every page and joins the text into one extraction input. Gemini extraction sends the complete PDF as one uploaded/inline document. There is no selected-page-range field in batch/job/source identity.
- A single extraction may yield multiple recipes, but an identical title on different pages has no page-specific provenance. Raising only the file/page bounds would increase memory, OCR/provider work, and identity ambiguity without adding resumable page-level ownership.

The current workflow is suitable for recipe-sized scans and PDFs within these bounds. Whole-cookbook input is **Unverified / Unsupported** by the current page-range workflow; a synthetic 101-page PDF is rejected without adding a batch source.

### Deferred implementation design

Page-range support needs a durable document-to-range identity model, so it is deferred rather than implemented as a limit increase.

1. Keep the original PDF immutable and identify it by its full SHA-256, page count, and accepted storage path.
2. Add an explicit selected range `(original_hash, first_page, last_page)` as a child source. Give every range a deterministic ID derived from the original hash and inclusive page bounds; store parent book ID and page provenance in batch/job events.
3. Create a bounded temporary PDF containing only the selected pages with PyMuPDF page-copy operations. Validate page bounds and output size before extraction; always clean up task-owned temporary files after completion or recovery.
4. Include original hash, range bounds, model/prompt/schema configuration, and extraction version in the range cache identity. Preserve prior range caches and attempt records on retries.
5. Bind each candidate to the parent document hash and page range as well as its extraction identity. Do not use title or array position to match candidates across ranges or re-extractions.
6. Treat overlapping ranges as separate extraction evidence until a reviewer compares their source locations and content. Provide explicit duplicate/reconciliation decisions; never suppress a recipe solely because the title matches.
7. Resume by range and recipe outcome. Completed ranges and successful recipes remain protected; interrupted or failed ranges can be retried without replacing the original book or prior outputs.
8. Add a review UI for page thumbnails, range entry/selection, and page-bound provenance. Keep range selection explicit before extraction and show the source pages beside every candidate.

Before release, add synthetic PDFs with more than 100 pages, duplicate titles on different pages, overlapping ranges, multi-recipe ranges, interrupted range retries, changed source bytes, and bounded-memory rendering. Verify old whole-file batch records remain readable and no original PDF is rewritten.
