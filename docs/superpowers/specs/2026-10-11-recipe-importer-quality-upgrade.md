# Recipe-Importer Quality and Automation Upgrade

## Intent

Make the existing PDF/JPG-to-Gemini-to-Mealie importer safer to resume and more useful when scans are incomplete, while preserving the existing cache, append-only import ledger, CLI modes, and no-duplicate behavior.

## Goals

- Keep source transcription separate from diagnostics and unverified AI proposals.
- Provide a local, auditable review queue whose pending proposals cannot reach Mealie.
- Retry only unresolved recipes and reconcile multi-recipe files by stable candidate identity rather than array position.
- Route calls through an ordered, configurable model list with global/per-model rate and daily accounting.
- Add idempotent batch-tagging and an explicit tag-backfill operation.
- Keep live-service behavior behind small clients and cover it with mocks.

## Non-goals

- No web review UI, OCR engine, recipe rewriting, or automatic application of proposals.
- No cookbook-membership endpoint invented; tags are the supported grouping primitive.
- No destructive rewrite of `output/import_state.jsonl`, cached extractions, source files, or prior Mealie recipes.
- No live bulk import or live API verification during development.

## Decisions

- Python standard library plus the existing dependencies remain the implementation stack.
- New extraction attempts are stored as additional cache files containing source hash, model, prompt/schema version, and attempt metadata; legacy `<hash>.json` caches remain readable.
- Legacy `hash:index` state keys are still honored. New records include a stable source/title identity and are matched uniquely; duplicate-title ambiguity is sent to review.
- Review decisions are append-only JSONL events. CSV is an actionable projection, not the authority.
- Model configuration accepts JSON in `GEMINI_MODELS_JSON`, with the existing `GEMINI_MODEL` as a backward-compatible single-model fallback. Local quotas are upper bounds and explicitly best-effort against external project traffic.
- Tagging fetches the existing recipe/tag representations, merges the requested tag by Mealie tag identity, and records recipe and tag outcomes separately.

## Success boundary

Existing successful state records remain skip-safe; pending or rejected suggestions never affect the Mealie payload; an approved/edited proposal is only eligible through an explicit review-application action; and every unverified live-service behavior is called out in the handoff.
