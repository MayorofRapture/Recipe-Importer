---
name: import-safety
description: Protect Mealie imports when implementing resumable recipe batches, cached PDF/JPG extractions, retry-incomplete workflows, multi-recipe files, review approvals, and batch cookbook tags. Use for any changes to persistent state, duplicate prevention, or Mealie API writes.
---

# Import Safety — Recipe-Importer

## Scope

Activate for writes to Mealie, state/caching changes, report generation, batch tags, or any retry that touches previously processed source files. Follow root `AGENTS.md`. Use `recipe-extraction-quality` when a proposed missing-detail completion is involved.

## Never break these invariants

- Existing source files and `output/` are user data. Do not delete, overwrite, truncate, or relocate them on a routine run.
- Treat `output/import_state.jsonl` as a durable ledger, not a disposable progress indicator. Keep backwards compatibility; back up before migrations.
- Imported recipe content must derive from **observed source data**, or from a separate suggestion that was **explicitly approved/edited** by a human, with provenance. Pending suggestions must not enter Mealie.
- Re-running must not duplicate successes; ambiguous timeouts or unexpected 201 payloads may indicate an already-created recipe. Record uncertainty and reconcile before reposting.
- A JPG/PDF may contain multiple recipes. Re-extraction can reorder, split, or combine them. **File hash + array index is not a stable identity across re-extractions**; do not associate a proposal or approval with a different recipe after reordering.
- Run API tests with mocks/fakes unless the user specifically authorizes live changes.

## State model and reconciliation

Before changing persistence, inspect the current on-disk schema and use a compatibility layer. Prefer per-source IDs with content fingerprints/verified title matching plus human reconciliation when ambiguous. Store source hash, recipe identity, extraction attempt/model/schema version, Mealie slug, import state, tag state, and reviewer decisions. Maintain links from suggestions to the exact source evidence and candidate identity.

Suggested lifecycle concepts (map to actual code):

```text
EXTRACTED -> VALIDATED -> [NEEDS_REVIEW -> APPROVED/EDITED | REJECTED]
                        -> READY_TO_IMPORT -> IMPORTED -> TAGGED
                                    \-> IMPORT_UNCERTAIN -> RECONCILE
```

- `READY_TO_IMPORT` means the authoritative recipe is complete; it does **not** imply Gemini suggestions were approved.
- For an approved/edited suggestion, import/update only through an explicit review-application operation, ideally with a preview/dry-run and stored diff. Do not silently change already-imported recipes.
- When extraction fails or a source is genuinely missing content, continue processing other files; log why.
- Allow `--extract-only`, limited smoke tests, and selective retries; maintain idempotency across interruptions and restarts.

## Batch tagging / cookbook organization

The user needs to set a **batch tag** before import to group recipes into a Mealie cookbook. In Mealie, cookbooks can be configured as tag/category filters; verify against the user's Mealie version before writing API code. Treat `--batch-tag "..."` (or equivalent `.env` setting) as the user's explicit choice. Avoid changing historical imports when a later batch selects a different tag.

Separate outcomes and retries for:

1. Recipe creation (can succeed even if subsequent tagging fails).
2. Tag existence/creation.
3. Tag association without removing existing tags.
4. Optional cookbook filter setup (only if explicitly authorized and supported).

Never repeat recipe creation solely to repair tagging. Offer an explicit, narrowly scoped backfill command for existing recipes; require user confirmation before mass updates.

## Testing checklist

- Existing import-state log and JSON caches load with no destructive migration.
- A two-recipe image: first already imported; second incomplete; retry imports **only** the intended second recipe.
- Two extraction attempts reorder recipes; candidate identities are reconciled or held for review.
- HTTP timeout after successful remote create -> uncertain state, **no automatic duplicate POST**.
- Mealie create succeeds, tag operation fails -> rerun repairs the tag without creating another recipe.
- Existing tags survive batch-tag addition; changing batch tag later does not silently retag old imports.
- Pending/rejected suggestions are excluded from Mealie payloads; approved/edited suggestions are traceable and require an explicit action.
- No API secrets or raw private recipe contents in committed files.

At completion, report exactly which tests ran, the observed results, and any behavior still unverified against live Mealie.
