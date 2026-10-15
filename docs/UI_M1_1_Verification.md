# M1.1 Review UI verification

## Scope

This record covers the Review workspace redesign only. It uses sanitized temporary
fixtures and does not call Gemini, Mealie, or modify `output/`, extraction caches,
review history, source scans, or `.env`.

## Implemented checks

- Native top navigation exposes Upload, Review, and Import & history.
- Review queue controls stay together above the selected recipe: search, batch and
  status filters, progress, and previous/next navigation.
- Structured Edit is the default for new candidates; Quick Edit remains available.
- Ingredients and instructions support explicit edit, add, remove, and reorder
  controls.
- Pending suggestions remain inline, labeled as unverified, and separate from the
  authoritative recipe. Approve & next is disabled while critical findings or
  pending suggestions remain.
- Save draft, Save & next, Approve & next, Skip, and Previous remain visible near
  the recipe heading. Failed writes retain the current recipe.
- Source and editor use bounded containers for independent scrolling at desktop
  sizes. Source images include an accessible alternative description.
- Queue advancement reaches an explicit end state instead of wrapping to the first
  recipe.

## Automated evidence

Run from PowerShell with the repository virtual environment:

```powershell
& .\.venv\Scripts\python.exe -m unittest tests.test_streamlit_navigation tests.test_streamlit_review tests.test_streamlit_structured_review tests.test_streamlit_responsive_review -v
```

Result: **Verified** — 9 tests passed.

The responsive AppTest checks bounded containers, source alternative text, and the
approval enabled/disabled contract. Existing navigation, draft, structured-edit,
and inline-suggestion tests also pass.

Additional local checks:

- **Verified** — `& .\.venv\Scripts\python.exe -m unittest discover -v`: 91 tests passed.
- **Verified** — `& .\.venv\Scripts\python.exe -m compileall -q app_pages ui_data.py ui_review_state.py tests`.
- **Verified** — `& .\.venv\Scripts\python.exe import_recipes.py --help`.
- **Verified** — Streamlit started against a temporary sanitized fixture and returned
  `HTTP/1.1 200 OK` on `127.0.0.1:8511`; the server was stopped afterward.

## Browser and accessibility evidence

**Unverified** — a real-browser Playwright run and screenshots were not available
in this Windows environment because the Playwright CLI/browser package was not
available from the local package cache. AppTest verifies Streamlit widget state and
labels but cannot prove browser focus order, rendered reflow, contrast, or DOM
accessibility relationships.

Before release, run the happy path and a failed-save/recovery path at 1440x900,
1366x768, and a narrow viewport; capture screenshots; then run the accessibility
audit against the rendered page. Treat those results as separate browser evidence,
not as a substitute for the deterministic safety tests above.

## Safety boundary

No live Gemini or Mealie behavior is claimed as verified. The redesign remains a
presentation/orchestration layer over the existing importer, review ledger, draft
store, and candidate contracts.
