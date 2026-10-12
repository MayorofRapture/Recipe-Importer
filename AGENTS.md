# Recipe-Importer — Codex Project Instructions

## Mission

Maintain and improve a **small, reliable, resumable** Python pipeline that imports local PDF/JPG recipe scans into Mealie via Gemini extraction and the Mealie REST API. Prioritize accurate recipes, quick batch processing, predictable recovery, and avoiding duplicates. Do not turn this into a general ETL framework.

## Project context

- Repository: `MayorofRapture/Recipe-Importer`.
- Development environment: Windows / PowerShell, Python virtual environment (`.venv`).
- Production destination: user-hosted Mealie on NAS01; use `MEALIE_URL` and `MEALIE_TOKEN` from `.env`, never hard-coded credentials.
- Existing pipeline: source PDFs/JPGs -> Gemini structured extraction -> cached JSON -> Schema.org Recipe data -> Mealie API.
- Known entry point: `import_recipes.py`; first inspect actual repository files before assuming that names, behavior, or API contracts are unchanged.
- Existing artifacts include `output/extracted/`, `output/import_state.jsonl`, and `output/last_run_report.csv`.
- Initial batch report (October 2026): `IMPORTED=327`, `ALREADY_DONE=5`, `INCOMPLETE=38`, `EXTRACTION_FAILED=12`, `NO_RECIPE=1`. These are historical observations, not a live count or a test fixture.
- Some source files contain **two or more distinct recipes**. One file can legitimately produce multiple Mealie entries.

## Working style and installed skills

1. Keep changes focused and minimal; prefer small, independently testable steps.
2. Inspect `README.md`, `import_recipes.py`, `.env.example`, existing tests, and `.agents/skills/` before proposing changes.
3. Follow applicable installed **Superpowers** and **Ponytail** skill instructions. For design use brainstorming/planning where applicable; for implementation use test-driven development, systematic debugging, and verification. Use review/audit skills for final checks. Do not assume optional hooks or exact skill names are available without checking the installed folders.
4. Prefer the smallest maintainable change consistent with the requirements. Avoid unnecessary dependencies, abstractions, and unrelated refactors.
5. Explain the intended changes concisely before modifying high-risk import, persistence, or API behavior.
6. Work on the user's existing development branch. Do not merge, force-push, rewrite history, delete source files, or commit/push unless requested.
7. Keep all examples and commands compatible with PowerShell on Windows. Use `python` once `.venv` is active, or `\.venv\Scripts\python.exe` without activation.

## Non-negotiable invariants

- **Do not invent recipe content.** Extract only what is supported by the image/PDF. Preserve original quantities, units, oven temperatures, durations, ordering, ingredient wording, and instructions.
- **Distinguish source omissions from extraction failures.** A scan can be perfectly legible yet omit required information; a stronger model cannot recover information that is absent from the source. Never use model escalation as permission to fabricate missing steps or quantities.
- **Never silently duplicate imported recipes.** Preserve existing state files and identifiers. A retry must not re-import already successful entries, including when only one recipe within a multi-recipe image failed.
- **Do not treat an ambiguous Mealie response as a definite failure.** On network timeout or uncertain server outcome, flag for reconciliation instead of blindly reposting.
- **Do not discard original source files or cached extractions.** Preserve useful prior output when retrying, and keep provenance (source path/hash, model, extraction attempt, status) inspectable.
- **Keep secrets out of the repository and logs.** Never commit `.env`, API keys/tokens, source scans, private recipe contents, or `output/` unless explicitly authorized. Redact credentials from error messages.
- **No live destructive or bulk actions in tests.** Mock Gemini and Mealie by default; require explicit user approval for tests against the real services.

## Approved change scope (development branch)

### 1. Extraction diagnostics and quality control

- Record, **per recipe**, what is `missing_from_source`, `unreadable`, `ambiguous`, or `not_extracted` (the latter means visible evidence appears present but parsing failed).
- Provide concise human-readable details and location/evidence where possible, plus suggested action. Never guess missing values.
- Separate **critical** data (e.g., missing ingredient quantities when needed, missing essential direction/time/temperature) from **optional** metadata (e.g., yield absent from the original). Optional fields must not automatically make a recipe incomplete.
- Add a `NEEDS_REVIEW` workflow and an actionable `needs_review.csv` while preserving existing status meanings and reports.
- A recipe may have both ingredients and instructions and still need review because crucial details are missing or ambiguous.

### 2. Safe retries and model escalation

- Make model candidates a **user-configurable ordered list**, with distinct roles for low-cost primary extraction, quality escalation, and quota fallback. Validate model IDs using the provider rather than assuming a version name exists.
- Use a stronger model when extraction quality or schema validation justifies it; a quota fallback is not automatically a quality escalation.
- Support provider-enforced request/token/day limits as configured or observed. **Do not claim to bypass shared account/project quotas by switching models.** Handle `429` and transient failures with bounded backoff; stop or report cleanly when no eligible model is available.
- Rate-limit scheduling must account for all workers; do not fire a new request above configured limits. Preserve enough per-model usage state across restarts to reduce accidental daily-limit overruns, with clearly documented limitations (local counters cannot know traffic from other applications).
- Implement `--retry-incomplete` or an equivalent explicit operation that targets unresolved files/recipes only. Preserve prior extraction versions and already-imported recipes.
- **Identity safety:** never rely solely on position in Gemini's `recipes[]` array across re-extractions; it may reorder or split recipes. Match/reconcile candidates carefully, and send uncertain cases to review instead of creating duplicates.

### 3. Batch cookbook tagging

- Allow a **user-specified batch tag before import** (CLI option or `.env` setting), applied to every successfully created recipe in that batch; preserve other tags and avoid destructive overwrites.
- Mealie **Cookbooks are saved recipe filters** built from tags/categories/tools; they are not necessarily direct recipe membership. Implement a batch tag that can be used as the corresponding cookbook's filter. Do not invent a direct cookbook-assignment endpoint.
- Confirm behavior against the installed Mealie version and its API; make tag creation/lookup and association idempotent, including retries where recipe creation succeeded but tag application did not.
- Do not silently relabel previously imported recipes when the batch tag changes. Make backfill/retag an explicit, scoped operation with user approval.
- Record both recipe import and tagging outcomes separately so partial success is recoverable.

## Storage, reporting, and compatibility

- Treat `output/import_state.jsonl` as valuable persistent history. **Do not reset, rewrite, or migrate it destructively.** If a schema migration is necessary, create a backup, document it, and test against legacy records.
- Cache identity/versioning should account for file hash and extraction configuration (model/prompt/schema version), so an intentional retry does not silently use stale output. Preserve prior cache versions when practical.
- Support a no-import extraction mode (`--extract-only`) and a small `--limit` smoke test.
- Reports should make failures actionable: source file, recipe title/identity, model tried, status, missing-data diagnosis, and next step. Do not leak scanned contents or secrets unnecessarily.
- Existing successfully imported recipes must remain safe to rerun without duplicate POSTs.
- Validate the actual Mealie request/response contract before altering payloads or status handling; avoid assumptions based on latest Mealie docs that may differ from the user's installed release.

## Test requirements

Write automated tests **before** behavior changes when practical, and verify locally without network access. Cover at minimum:

- One image -> one recipe; one image -> multiple recipes.
- A multi-recipe image where one recipe was imported and another needs retry.
- A legible scan that genuinely lacks an ingredient quantity or instruction: report missing source data without inventing it.
- Unreadable/cropped source; optional metadata absent; critical detail ambiguous.
- Primary model failure -> eligible fallback; quality escalation; all models quota-exhausted; transient/backoff behavior; restart and local counters.
- Cached extraction re-use, intentional cache re-extraction, and backward compatibility with existing state/history.
- Mealie create succeeds, fails definitively, or has an ambiguous response; ensure no blind duplicate creation.
- Tag exists, tag missing, tag API failure after recipe creation, tag backfill, and preservation of pre-existing tags.
- Secret-redaction and user-facing `needs_review.csv` / run-report correctness.

Run `python -m pytest` if pytest is configured; otherwise use the repository's actual test command. Run `python -m compileall -q .` or a targeted syntax check and verify CLI `--help`. Report commands run, passing/failing counts, and untested live behavior. Never claim real Mealie/Gemini integration is verified solely from mocks.

## Implementation sequence

1. Read current code, discover installed skills, establish a passing baseline, and write a concise risk-aware implementation plan.
2. Add structured extraction diagnostics and review reporting first.
3. Implement safe targeted retries and candidate reconciliation.
4. Implement configurable model routing, throttling, and quota-aware fallback.
5. Implement batch tagging and reconciliation with Mealie cookbooks.
6. Add/update tests and usage documentation; provide a minimal dry-run/smoke-test procedure.

**Definition of done:** On a repeat run against the existing first-batch state, previously imported recipes are not recreated; incomplete cases are explained; eligible retries use configured models responsibly; multi-recipe files stay correctly matched; and newly imported recipes receive the selected batch tag with recoverable partial failures.
