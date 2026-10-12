# Recipe-Importer Quality and Automation Upgrade Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Upgrade the existing importer with source diagnostics, human-reviewed suggestions, safe selective retries, configurable model routing, and recoverable batch tagging without duplicating existing recipes.

**Architecture:** Keep `import_recipes.py` as the CLI/orchestration boundary and move durable concerns into small Python modules: `recipe_quality.py`, `review_store.py`, `model_routing.py`, and `mealie_client.py`. Load legacy caches/state through compatibility functions, write new attempts/events additively, and make all provider behavior injectable in tests.

**Tech Stack:** Python 3.11+, Pydantic, google-genai, requests, python-dotenv, unittest, JSONL/CSV.

**Spec:** `docs/superpowers/specs/2026-10-11-recipe-importer-quality-upgrade.md`

## Global Constraints

- Preserve source files, `output/`, legacy JSON caches, and append-only `output/import_state.jsonl`.
- Authoritative recipe fields contain only source-supported transcription or explicitly approved/edited review values.
- Pending/rejected suggestions never enter a Mealie payload.
- Preserve `--extract-only`, `--limit`, `--workers`, and `--rpm`; add explicit retry/review/tag operations without live defaults.
- Never log API keys, raw private scans, or provider error bodies containing secrets.
- Tests use fakes/mocks; no live Gemini or Mealie calls.

## Review Focus

- Legacy `hash:index` records and legacy raw cache JSON must load without migration or duplicate POSTs — Task 3 tests.
- Two same-file recipes reordered by a new extraction must match by unique identity, while duplicate titles become review — Task 3 tests.
- A legible omission must remain absent from authoritative fields even when a suggestion exists — Task 1 tests.
- A tag failure after recipe creation must be repairable without another recipe POST — Task 4 tests.
- Quota exhaustion across workers/restarts must stop or route deterministically without exceeding local limits — Task 2 tests.

### Task 1: Diagnostics, suggestions, and review ledger

**Files:**
- Create: `recipe_quality.py`
- Create: `review_store.py`
- Create: `tests/test_quality_review.py`
- Modify: `import_recipes.py`

**Interfaces:**
- `recipe_quality.py` provides `Diagnostic`, `Suggestion`, `RecipeCandidate`, `ExtractionBatch`, `schema_recipe`, `is_complete`, and `needs_review`.
- `review_store.py` provides append-only `ReviewStore`, `ReviewDecision`, `apply_review_decision`, and `reviewed_candidate`.

- [ ] Write failing tests for legacy model loading, omission diagnostics, pending/approved/edited/rejected decisions, CSV output, and exclusion of pending values from `schema_recipe`.
- [ ] Run `python -m unittest tests.test_quality_review -v` and verify the failures are about the missing new behavior.
- [ ] Implement the Pydantic models and prompt-compatible fields with defaults for legacy JSON.
- [ ] Implement append-only review events and an actionable `needs_review.csv`; keep proposed and approved values separate.
- [ ] Re-run focused tests, then test a complete recipe with optional metadata absent and confirm it is importable.

### Task 2: Configurable model routing and persistent usage

**Files:**
- Create: `model_routing.py`
- Create: `tests/test_model_routing.py`
- Modify: `import_recipes.py`, `.env.example`

**Interfaces:**
- `ModelConfig`, `parse_model_config`, `UsageLedger`, `ModelScheduler`, and `route_extraction` are the public seams.
- The scheduler must reserve request starts globally and per model before provider calls and record attempts after restart.

- [ ] Write failing fake-client tests for primary success, quality escalation, quota fallback, all-model exhaustion, transient bounded retry, and restart accounting.
- [ ] Run the focused tests and verify red failures.
- [ ] Implement JSON/env parsing, provider model-list validation seam, UTC/best-effort usage persistence, and bounded scheduling.
- [ ] Integrate model/attempt metadata into additive cache envelopes and ensure legacy caches remain usable.
- [ ] Re-run focused tests and assert no secrets appear in route/error records.

### Task 3: Stable identity and selective retry

**Files:**
- Create: `tests/test_reconciliation.py`
- Modify: `import_recipes.py`, `review_store.py`

**Interfaces:**
- `StateStore.find_candidate(source_hash, index, candidate)` returns a unique prior record, no match, or an ambiguity result.
- `candidate_identity(source_hash, candidate)` is deterministic for a source/title/content candidate.

- [ ] Write failing tests for legacy state compatibility, imported-first/incomplete-second multi-recipe retry, reorder matching, ambiguous duplicate titles, forced re-extraction, and no duplicate Mealie create.
- [ ] Run focused tests and verify red failures.
- [ ] Implement additive state fields, unique identity matching, `--retry-incomplete`, and `--force-extract` cache behavior.
- [ ] Ensure only unresolved candidates are sent to extraction/import and uncertain outcomes remain non-retriable until reconciliation.
- [ ] Re-run focused tests and the complete unittest suite.

### Task 4: Mealie tags and explicit backfill

**Files:**
- Create: `mealie_client.py`
- Create: `tests/test_mealie_tags.py`
- Modify: `import_recipes.py`, `.env.example`

**Interfaces:**
- `MealieClient.create_recipe`, `ensure_tag`, `add_tag`, and `backfill_tag` keep recipe creation and tag association separate.
- Existing tags are merged by Mealie tag identity; tag retry never calls recipe creation.

- [ ] Write failing fake-session tests for existing/missing tags, preserved existing tags, tag failure after create, and scoped backfill.
- [ ] Run focused tests and verify red failures.
- [ ] Implement the client against configurable Mealie v3 organizer/recipe endpoints and preserve HTTP ambiguity rules.
- [ ] Integrate `--batch-tag` and an explicit `--backfill-tag` mode with dry-run confirmation/preview.
- [ ] Re-run focused tests and document that live endpoint compatibility remains unverified without the user’s Mealie instance.

### Task 5: CLI, reporting, documentation, and final verification

**Files:**
- Modify: `import_recipes.py`, `README.md`, `.env.example`
- Create/update: `tests/test_cli_reporting.py`

- [ ] Add `NEEDS_REVIEW`, route/model, diagnosis, tag, and reconciliation fields to run reports without changing existing statuses.
- [ ] Add CLI help and PowerShell examples for review decisions, selective retry, batch tagging, and tag backfill.
- [ ] Run `python -m unittest discover -v`, `python -m compileall -q .`, `python import_recipes.py --help`, and `git diff --check`.
- [ ] Review the complete diff for protected-path/output/secret violations and report live behavior as unverified unless actually authorized and executed.
