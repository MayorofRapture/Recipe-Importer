# Mealie quick recipe importer (PDF/JPG → Gemini → Mealie)

Designed for **300+ files in one short session**. Each file usually contains one recipe; if Gemini finds several distinct recipes in one file, each can be imported. Uses **Mealie's non-AI JSON import endpoint**, so you do not need to configure AI within Mealie. Tested for Python syntax and local mocked workflow, **not** against your live Mealie/Gemini accounts.

> **Safe CLI default:** ordinary runs extract and register recipes for review only. They do not connect to Mealie or create recipes. Use `--import-approved` to import only exact revisions with current explicit approval. `--legacy-auto-import` deliberately enables the historical unattended behavior. Failed tag repair is separate and requires `--repair-failed-tags`.

## First-time setup (Windows PowerShell)

1. Install Python 3.14 for the checked-in lock. The app also supports Python 3.11+ with the bounded `requirements.txt`; Node.js 24 is needed only to run browser test harnesses. Unzip this folder anywhere on your Windows PC.
2. Put your PDFs/JPGs in a folder (`C:\Recipes_To_Import` in the example). Subfolders are fine; supported: PDF/JPG/JPEG/PNG/WEBP. The importer never moves or deletes your originals.
3. Get your **Gemini API key** from https://aistudio.google.com/apikey . Check rate limits and billing in AI Studio; a paid API quota may be necessary to finish 300 requests in an hour.
4. Create a **Mealie API token** in Mealie's profile/user settings. Your token must have permission to create recipes. Confirm you can open the `MEALIE_URL` from this PC.
5. In the extracted importer folder, create `.env` by copying `.env.example`; replace the two key placeholders and set `RECIPE_DIR` to your recipe folder. **Do not share `.env` or commit it to Git.**
6. In PowerShell from the importer folder:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-lock.txt
.\.venv\Scripts\python.exe import_recipes.py --extract-only --limit 5
```

Review the extracted candidates in the local app, save and approve the exact
revision, then authorize only those approvals from the CLI with
`--import-approved`. Use `--legacy-auto-import` only when unattended import is
intended. The old bare command no longer imports recipes. The execution mode is
printed before batch processing; ordinary review-only mode requires no Mealie
credentials and performs no Mealie preflight.

`requirements-lock.txt` pins the Python 3.14 environment observed in the
verified development venv; optional `pytesseract` stays in
`requirements-ocr.txt` and is constrained by this lock. To update the lock,
install intended package changes from `requirements.txt`, run the offline suite
on Windows, regenerate it from a core-only environment with
`python -m pip freeze`, omit the optional `pytesseract` line, then review every
version change. `requirements.txt` keeps broader compatible ranges for other
supported Python versions. CI installs only the lock and never reads `.env`.

Local OCR recovery is optional. Install its Python dependencies only when you
want recovery after a Gemini `finish_reason=RECITATION` failure:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-ocr.txt -c requirements-lock.txt
# Install Tesseract for Windows, then configure its executable if it is not on PATH.
$env:TESSERACT_CMD = 'C:\Program Files\Tesseract-OCR\tesseract.exe'
```

Selectable PDF text uses PyMuPDF. JPG/PNG/WEBP files and scanned PDF pages use
Tesseract. OCR text is saved for inspection and always enters manual review;
it is never imported automatically.

To run local OCR directly without initializing Gemini or connecting to Mealie:

```powershell
.\.venv\Scripts\python.exe import_recipes.py --ocr-only --source C:\Recipes_To_Import --limit 5 --workers 2
```

This writes raw text to `output/ocr_text/`, records `OCR_NEEDS_REVIEW` or
`OCR_FAILED` in `last_run_report.csv`, and updates the existing review ledger.
Repeated runs reuse the digest-named OCR text and do not append duplicate review
records.

## Local review app

See [identity stability and reconciliation](docs/identity-stability-and-reconciliation.md) for the versioned matching rules and safe recovery workflow.
See [manual approval and recipes without instructions](docs/manual-approval-and-no-instructions.md)
for the explicit source-unavailable approval path, revision-bound no-instructions
decisions, and Mealie endpoint verification limits.

After installing the project dependencies, double-click `run_app.bat` or start
the app from PowerShell:

```powershell
.\.venv\Scripts\python.exe -m streamlit run streamlit_app.py
```

The app opens Upload, then keeps extraction results in the local output library.
Create a named batch, optionally choose its cookbook tag, add scans, and select an
extraction method. Local OCR stays on this computer. Gemini extraction sends scans
to Google; OCR-assisted review also sends the selected scan and OCR text. Both
cloud modes require explicit consent. Pause/resume keeps completed files and retries
only unfinished files.

Use Review to compare each candidate with its source scan. Committed editor fields
save locally as a draft; bulk edits take effect only after Apply. Inspect every
unverified proposal and source issue, then approve a specific saved revision. Save,
approval, and import are separate actions. Pending proposals and unapproved retry
results cannot be imported.

Review also flags ingredient rows that look like preparation instructions,
substitution notes, or several measured ingredients combined by OCR. These are
heuristic warnings: compare them with the source before changing anything. Use
**Review ingredient extraction** to edit one suggested reorganization of ingredients,
instructions, and notes. Saving the proposal keeps it as a local draft; only **Apply
proposed repair** creates a new review revision, which must still be reviewed and
approved. Imported and uncertain recipes stay read-only. **Extraction quality** in
the Review queue audits current effective recipe revisions and includes imported
recipes as read-only results; opening the audit does not write review state. Notes and
substitutions remain outside ingredient rows and are carried into Mealie's recipe
description.

In Instructions, use **Suggest missing instructions with AI** when there are no usable steps, or **Suggest improvements with AI** when instructions already exist. Each request requires fresh consent and sends only the recipe title, ingredients, and current instructions as text to Gemini; it does not send the scan. If the scan is unavailable, the page explains that suggestions are inferred from recipe text and must be reviewed as unverified. The durable job runs in the background, and its result stays labeled inferred and unverified until you accept or edit it. Review the current wording, proposed wording, difference, and uncertainty together beside the relevant step. Declining an optional generated completion leaves the recipe unchanged; critical source findings remain separate. If a proposal is stale, compare it with the current wording and explicitly apply it only when its target still exists, or discard it before requesting again with fresh consent.

The Review panel summarizes suggestion decisions and shows each approval blocker beside a direct Review action. This keeps the next correction close to the reason approval is unavailable. Applied proposals remain distinguishable from accepted edits and declined suggestions, while rejection leaves the source issue visible until a separate follow-up is recorded. If the recipe changes while a generated proposal is pending, compare its original wording with the current wording; explicitly apply it to the current revision when its target still exists, or discard it and request a new proposal with fresh consent.

Use **Back** to revisit the prior recipe, **Skip for now** to move it to the persistent Skipped recipes list, and **Return to** to reopen that exact recipe. If its status no longer matches saved queue filters, Review adjusts the filters and explains the change. A queued AI request remains visible after reopening the app; resume or cancel it explicitly. If a worker may have stopped during a provider call, the app does not retry automatically because the provider outcome is unknown. Start another request only with fresh consent.

If a source preview is unavailable, use **Locate the original PDF or image** in
Review to reconnect the exact original bytes. The app verifies its content hash;
a same-named file with different bytes cannot restore or approve that recipe.
When the original is permanently unavailable, Review offers a separate manual
approval that requires an acknowledgment and rationale and records that it was
not verified against the scan. Recipes with no preparation steps remain
incomplete until you record an exact-revision reason that none are required.
Import preflight blocks empty-instructions recipes until installed Mealie
support is verified and `MEALIE_SUPPORTS_EMPTY_INSTRUCTIONS=true` is set.

Use Import & history to select approved recipes and review the exact preflight
before starting a Mealie import. The selected batch tag applies only to recipes
created from that batch; it does not retag older recipes. If a request has an
uncertain server outcome or a partial tag failure, check the recorded result and
reconcile it before retrying. The importer avoids blindly repeating uncertain
creates.

When a re-extraction produces an unmatched candidate from a source that already
has an imported or uncertain sibling, the candidate stays held for explicit
mapping or deferral. The current source model has no independent recipe-boundary
anchor, so it cannot safely support declaring that unmatched candidate new in
that protected-source case.

Synthetic browser/testing mode requires `RECIPE_IMPORTER_FIXTURE_MODE=1` and an
explicit isolated `RECIPE_IMPORTER_OUTPUT_DIR` containing a `.fixture-library`
marker. It stops when that boundary is missing or invalid, uses synthetic results,
and disables live service clients. The normal launcher does not enable fixture
mode.

The default local library is `output/`. To keep it elsewhere, set
`RECIPE_IMPORTER_OUTPUT_DIR` before launching. Set `RECIPE_IMPORTER_SOURCE_DIR`
only when scans are stored separately from the output library. Keep those paths
local and do not commit recipe scans, `.env`, or output records.

Run the complete offline regression suite from PowerShell:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m compileall -q -x '(^|[\\/])(\.git|\.venv|\.agents|\.test-tmp|output)([\\/]|$)' .
.\.venv\Scripts\python.exe import_recipes.py --help
node --check tools/browser_review_acceptance.mjs
node --test tests/browser_fixture_runner.test.mjs
```

The Windows GitHub Actions workflow runs these offline checks with fresh Python
and Node environments, plus synthetic fixture setup. It needs no Gemini or
Mealie credentials. The broad browser acceptance is run locally with
`node tools/browser_review_acceptance.mjs`; it starts only its owned, attested
synthetic Streamlit process and needs cached Playwright Chromium.

To structure the saved/local OCR into reviewable recipe JSON, using deterministic
Python parsing first and a text-only Gemini line-reference request only when the
boundaries are ambiguous:

```powershell
.\.venv\Scripts\python.exe import_recipes.py --ocr-structure --source C:\Recipes_To_Import --limit 5 --workers 2
```

This mode never connects to Mealie or sends the image/PDF to Gemini. If the local
parser cannot safely identify recipe sections, Gemini receives only numbered OCR
text and returns source line ranges; Python assembles the original text locally.
Structured output is saved under `output/extracted/` and remains pending manual
review. Approve a complete OCR candidate before a later normal import run:

```powershell
.\.venv\Scripts\python.exe import_recipes.py --review-id "SOURCE_HASH:RECIPE_ID" --review-status approved
```

To regenerate a Fried Rice structured cache after a parser fix, place only that
source in a temporary development folder and run:

```powershell
.\.venv\Scripts\python.exe import_recipes.py --ocr-structure --source C:\Recipe_Dev\fried-rice --limit 1 --workers 1 --force-extract
```

`--force-extract` creates a new structured cache while retaining older caches,
the raw text under `output/ocr_text/`, review approvals, and import history. A
deterministically structured source does not make a Gemini request and this mode
does not connect to Mealie.

Layout OCR keeps three versioned text files for each source: the unmodified
Tesseract/PyMuPDF stream, the coordinate-ordered stream, and the selected stream.
The selected stream is used for structuring only when its quality checks are at
least as reliable as the original; otherwise the original is retained and the
layout concerns are sent to manual review.

To safely regenerate and compare both variants for two files, copy each source
into its own temporary development folder and run OCR-only:

```powershell
$sourceRoot = 'C:\Path\To\RecipeScans'
$devRoot = Join-Path $env:TEMP 'recipe-import-ocr-compare'
New-Item -ItemType Directory -Force -Path $devRoot | Out-Null

foreach ($file in @('Scan_20260907_155742.jpg', 'Scan_20260907_161736.jpg')) {
  $oneSource = Join-Path $devRoot ([IO.Path]::GetFileNameWithoutExtension($file))
  New-Item -ItemType Directory -Force -Path $oneSource | Out-Null
  Copy-Item -LiteralPath (Join-Path $sourceRoot $file) -Destination $oneSource
  .\.venv\Scripts\python.exe import_recipes.py --ocr-only `
    --source $oneSource --limit 1 --workers 1
  $row = Import-Csv .\output\last_run_report.csv | Select-Object -First 1
  Get-Content -LiteralPath $row.ocr_original_text_path
  Get-Content -LiteralPath $row.ocr_layout_text_path
  Get-Content -LiteralPath $row.ocr_selected_text_path
}
```

This writes only new OCR/review artifacts and does not call Gemini or Mealie.
Inspect the original and layout files against the source image before approving
anything for a later import run.

For an edited whole recipe, pass a JSON object containing only the fields to
replace (list fields preserve individual entries):

```powershell
.\.venv\Scripts\python.exe import_recipes.py --review-id "SOURCE_HASH:RECIPE_ID" --review-status edited --review-value '{"name":"Fried Rice","ingredients":["2 cups rice"],"instructions":["Cook rice.","Fold in eggs."]}'
```

The script first checks that your Mealie API token can authenticate. Open Mealie and spot-check the five recipes (especially quantities, temperatures, and instructions).

For unattended legacy imports (explicitly opt in):

```powershell
.\.venv\Scripts\python.exe import_recipes.py --legacy-auto-import
```

For the protected workflow, approve saved candidate revisions in the review UI
or with the review CLI, then import only those exact approvals:

```powershell
.\.venv\Scripts\python.exe import_recipes.py --import-approved
```

If stopped, **run the same command again**. Already-imported recipes will be skipped using the append-only import ledger and stable source/title identity; array reordering is reconciled and ambiguous duplicate titles are held for review.

### Optional commands

Extract 5 files without modifying Mealie:

```powershell
.\.venv\Scripts\python.exe import_recipes.py --limit 5 --extract-only
```

Try a lower request rate if receiving 429s:

```powershell
.\.venv\Scripts\python.exe import_recipes.py --legacy-auto-import --rpm 6 --workers 2
```

Retry unresolved files while preserving earlier extraction attempts:

```powershell
.\.venv\Scripts\python.exe import_recipes.py --extract-only --retry-incomplete --force-extract
```

Validate configured model IDs against the provider before extraction:

```powershell
.\.venv\Scripts\python.exe import_recipes.py --extract-only --limit 1 --validate-models
```

Record an explicit review decision. Pending and rejected proposals never enter Mealie; an approved/edited value is used only on a later import run:

```powershell
.\.venv\Scripts\python.exe import_recipes.py --review-id "SOURCE_HASH:RECIPE_ID" --review-suggestion "suggestion-id" --review-status edited --review-value "3/4 cup sugar"
```

Tag newly created recipes in a batch without retagging historical imports:

```powershell
.\.venv\Scripts\python.exe import_recipes.py --legacy-auto-import --batch-tag "Cookbook 2026"
```

Retrying a failed tag application does not create recipes. Run it as its own
explicit operation with `--repair-failed-tags --batch-tag "Cookbook 2026"`.

Repair tags only for an explicitly listed set of existing Mealie slugs; add `--dry-run` to preview:

```powershell
.\.venv\Scripts\python.exe import_recipes.py --backfill-tag "Cookbook 2026" --backfill-slugs recipe-one,recipe-two --dry-run
```

### Outputs

- `output/extracted/<sha256>.json`: raw structured extraction from Gemini, retained if the Mealie import fails; you can review warnings and correct fields here before rerunning.
- `output/import_state.jsonl`: durable audit of successful and **uncertain** imports, preventing most duplicate entries on rerun. **Do not delete** this file between runs.
- `output/last_run_report.csv`: status for each processed recipe, including `IMPORTED`, `ALREADY_DONE`, `IMPORT_FAILED`, `EXTRACTION_FAILED`, `INCOMPLETE`, and `NEEDS_MANUAL_CHECK`. Open the report in Excel.
- `output/review_events.jsonl`: append-only diagnostics, proposals, and human review decisions.
- `output/needs_review.csv`: actionable review projection with source, field, diagnosis, suggestion, uncertainty, and disposition.
- `output/gemini_usage.json`: atomic best-effort daily request reservations and shared request-start windows for CLI/UI workers that use the same output directory. Other applications using the provider project are not visible to this file.
- `output/gemini_diagnostics.jsonl`: append-only Gemini response metadata, including finish reason, attempted route, and any OCR fallback result.
- `output/ocr_text/`: raw local OCR text saved after a RECITATION failure. Review this text before approving or editing a recipe.
- `output/extracted/<sha256>--ocr-structure--*.json`: versioned, reviewable OCR structure assembled from original OCR lines; prior versions are retained.

If a file shows **NEEDS_MANUAL_CHECK**, inspect the recorded attempt and verify the recipe in Mealie before taking action. HTTP timeouts/5xx responses are ambiguous: the server might have created the recipe before the response failed, so the candidate remains protected from another create request. Use **Import & history** to record an explicit reconciliation when the result is known. If the outcome cannot be confirmed or the UI cannot safely reconcile it, leave the record protected and retain the log; never delete or rewrite `output/import_state.jsonl` to permit a retry. Contact the maintainer for a supported recovery path if needed.

### Important limitations

- This version **does not upload cover pictures or original PDFs to Mealie**, only recipe text and structure. Originals remain on disk. Cover/attachment import can be added later.
- Names, ingredients, and instructions are required; unusable extractions are flagged, not force-imported. Optional metadata such as yield does not make a recipe incomplete.
- Diagnostics distinguish source omissions, unreadable/cropped text, ambiguity, and visible-but-not-extracted content. Suggestions are unverified proposals and remain outside the authoritative recipe until a human approves or edits them.
- AI transcription can make mistakes; a quick 5-recipe test is essential, and recipes with extraction warnings need review.
- Exact duplicate input files are de-duplicated by SHA-256. Different files containing the *same recipe* can still create duplicate Mealie entries.
- API throughput depends on your Gemini project quotas (requests/minute, tokens/minute, requests/day), Mealie server load, source file sizes, and network access; the one-hour goal is **not guaranteed**.
- Local Gemini scheduling coordinates configured global/per-model requests per minute and request/day across importer workers sharing this ledger. Its day boundary is UTC, it cannot see external project traffic, and it does not enforce tokens/minute because the router has no reliable request-token estimate; provider limits remain authoritative.
- Gemini receives the PDF/image contents. Only upload materials you are authorized to process and comfortable sending to Google.
- Do not edit or delete extraction cache files to force a retry. Correct recipe content through Review, or use the documented `--force-extract` operation to create a new extraction version while retaining earlier caches and review/import history.
- Recipe import is best-effort against Mealie v3.22.x. If your Mealie configuration alters scraping behavior, test and troubleshoot before running all 300.

### Import mechanism

1. The configured Gemini model reads PDF or image **directly** (no OCR dependency, no PDF raster conversion). Model IDs are user configuration, not hard-coded catalog claims.
2. The Python script validates a constrained JSON response and converts only source-derived or explicitly approved/edited fields to `https://schema.org/Recipe` JSON-LD.
3. Python calls `POST /api/recipes/create/html-or-json` with a JSON body containing `data` as a **JSON-encoded string**, `includeTags=false`, `includeCategories=false`.
4. Mealie replies with a recipe slug; it is stored in `import_state.jsonl` before marking the import complete.

## Troubleshooting

- `401`/`403` from Mealie: check URL, token and permissions.
- `400`/`422` from Mealie: inspect the saved JSON extraction and `last_run_report.csv` for problematic recipes.
- `429` from Gemini: lower `GEMINI_RPM` or adjust your Google API quota/billing. The program automatically backs off on transient Gemini errors.
- `RECITATION` from Gemini is recorded without rotating to another model for the same source. If optional OCR dependencies are available, the importer saves local OCR text and marks the result for manual review. If Tesseract is not installed, set `TESSERACT_CMD` or review the saved diagnostic.
- Model routing uses `GEMINI_MODELS_JSON` in configured order. Quota fallback and quality escalation are separate decisions; local counters are best-effort and cannot see traffic from other applications in the same project.
- Mealie tags are used for cookbook-compatible filtering. Live tag endpoint compatibility and your installed Mealie version must be checked with a one-recipe dry run; no direct cookbook membership endpoint is assumed.
- If most files are huge multi-recipe cookbooks, this first-pass importer may need recipe boundary review.
