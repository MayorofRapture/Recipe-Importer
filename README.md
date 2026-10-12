# Mealie quick recipe importer (PDF/JPG → Gemini → Mealie)

Designed for **300+ files in one short session**. Each file usually contains one recipe; if Gemini finds several distinct recipes in one file, each can be imported. Uses **Mealie's non-AI JSON import endpoint**, so you do not need to configure AI within Mealie. Tested for Python syntax and local mocked workflow, **not** against your live Mealie/Gemini accounts.

## First-time setup (Windows PowerShell)

1. Install Python 3.11+ if necessary. Unzip this folder anywhere on your Windows PC.
2. Put your PDFs/JPGs in a folder (`C:\Recipes_To_Import` in the example). Subfolders are fine; supported: PDF/JPG/JPEG/PNG/WEBP. The importer never moves or deletes your originals.
3. Get your **Gemini API key** from https://aistudio.google.com/apikey . Check rate limits and billing in AI Studio; a paid API quota may be necessary to finish 300 requests in an hour.
4. Create a **Mealie API token** in Mealie's profile/user settings. Your token must have permission to create recipes. Confirm you can open the `MEALIE_URL` from this PC.
5. In the extracted importer folder, create `.env` by copying `.env.example`; replace the two key placeholders and set `RECIPE_DIR` to your recipe folder. **Do not share `.env` or commit it to Git.**
6. In PowerShell from the importer folder:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe import_recipes.py --limit 5
```

The script first checks that your Mealie API token can authenticate. Open Mealie and spot-check the five recipes (especially quantities, temperatures, and instructions).

Then import the rest:

```powershell
.\.venv\Scripts\python.exe import_recipes.py
```

If stopped, **run the same command again**. Already-imported recipes will be skipped using the append-only import ledger and stable source/title identity; array reordering is reconciled and ambiguous duplicate titles are held for review.

### Optional commands

Extract 5 files without modifying Mealie:

```powershell
.\.venv\Scripts\python.exe import_recipes.py --limit 5 --extract-only
```

Try a lower request rate if receiving 429s:

```powershell
.\.venv\Scripts\python.exe import_recipes.py --rpm 6 --workers 2
```

Retry unresolved files while preserving earlier extraction attempts:

```powershell
.\.venv\Scripts\python.exe import_recipes.py --retry-incomplete --force-extract
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
.\.venv\Scripts\python.exe import_recipes.py --batch-tag "Cookbook 2026"
```

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
- `output/gemini_usage.json`: best-effort local request accounting by UTC day and model; other applications using the same provider project are not visible to this file.

If a file shows **NEEDS_MANUAL_CHECK**, inspect Mealie for that recipe before attempting another import. HTTP timeouts/5xx responses are ambiguous: the server might have created the recipe before the response failed. Such entries are intentionally not retried automatically. If you determine that an uncertain recipe really was not created, you can edit its `UNCERTAIN` record out of `output/import_state.jsonl` while the script is stopped, then rerun. Preserve a backup of the log first.

### Important limitations

- This version **does not upload cover pictures or original PDFs to Mealie**, only recipe text and structure. Originals remain on disk. Cover/attachment import can be added later.
- Names, ingredients, and instructions are required; unusable extractions are flagged, not force-imported. Optional metadata such as yield does not make a recipe incomplete.
- Diagnostics distinguish source omissions, unreadable/cropped text, ambiguity, and visible-but-not-extracted content. Suggestions are unverified proposals and remain outside the authoritative recipe until a human approves or edits them.
- AI transcription can make mistakes; a quick 5-recipe test is essential, and recipes with extraction warnings need review.
- Exact duplicate input files are de-duplicated by SHA-256. Different files containing the *same recipe* can still create duplicate Mealie entries.
- API throughput depends on your Gemini project quotas (requests/minute, tokens/minute, requests/day), Mealie server load, source file sizes, and network access; the one-hour goal is **not guaranteed**.
- Gemini receives the PDF/image contents. Only upload materials you are authorized to process and comfortable sending to Google.
- Do not edit extraction cache files while the importer is running. To correct a failed cached extraction, edit its JSON after stopping, or remove just its cached JSON to force re-extraction.
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
- Model routing uses `GEMINI_MODELS_JSON` in configured order. Quota fallback and quality escalation are separate decisions; local counters are best-effort and cannot see traffic from other applications in the same project.
- Mealie tags are used for cookbook-compatible filtering. Live tag endpoint compatibility and your installed Mealie version must be checked with a one-recipe dry run; no direct cookbook membership endpoint is assumed.
- If most files are huge multi-recipe cookbooks, this first-pass importer may need recipe boundary review.
