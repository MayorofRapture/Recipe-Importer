# Gate C Test Reliability and Maintenance Plan

**Goal:** Make offline verification reproducible from a clean Windows checkout, repair the synthetic browser review flow, and document a measured, safe Gate D refactoring sequence.

**Architecture:** Keep Python `unittest`, Node's built-in test runner, the existing isolated browser fixture runner, and append-only JSONL formats. Replace local test-directory dependencies with scoped temporary directories, pin the verified Python environment in a lock file, and add one offline Windows CI workflow. Gate D is a separate roadmap only.

**Tech Stack:** Python 3.14.7, Streamlit 1.66.0, `unittest`/AppTest, Node.js 24, GitHub Actions, synthetic browser fixtures.

**Spec:** User attachment `Gate C: Test Reliability, CI, Documentation & Maintainability Planning` (2026-10-18).

## Global Constraints

- Preserve production `output/`, source scans, extraction caches, drafts, and Mealie records.
- No live Gemini or Mealie calls; browser workflows use the Gate A owned-process fixture runner.
- Do not merge PR #3 or execute the broad Gate D refactor.
- Keep JSONL histories append-only and compatible.
- Preserve the existing untracked `.vscode/` directory.

## Review Focus

- A clean checkout lacks ignored temp directories; tests must create and clean only their own state.
- Windows may lack directory-symlink privilege; keep the valid skip explicit and cover junction behavior.
- Browser queue changes must preserve the intended candidate identity through proposal actions, approval, and fake import.
- Dependency pins must match versions actually installed and tested on the supported Python runtime.
- CI and documentation must never require credentials or imply live-service verification.

---

## Baseline

- [x] Confirm branch `feature/complete-recipe-workspace`, baseline `23e04e7e600bbec2b2d31b6463756d15b4c9e869`, and upstream synchronization.
- [x] Preserve pre-existing untracked `.vscode/`.
- [x] Run baseline `.venv\\Scripts\\python.exe -m unittest discover -s tests -v`: 416 passed, 1 capability skip.
- [x] Record protected ledger hashes and current Python/package/Node versions.
- [ ] Run baseline broad browser acceptance and capture the precise drift point.

## Gate C

### Test portability and CI

- [x] Replace hard-coded `.devtest-tmp`, `.superpowers`, and persistent test fixture paths with scoped temporary directories; keep required browser evidence contained in `.test-tmp`.
- [x] Pin the verified development dependencies without inventing versions; document runtime versus optional OCR/Tesseract requirements.
- [x] Add a least-privilege Windows GitHub Actions workflow for compile, unit tests, CLI help, JS syntax, Node runner tests, and synthetic fixture setup.
- [ ] Verify from a clean archived checkout with no ignored test folders or production histories.

### Browser and documentation

- [x] Trace the broad flow's selected candidate through app queue state and fixture identity; strengthen assertions and diagnostics before consequential actions.
- [x] Complete the full synthetic review, decision-isolation, bulk-cancel, approval, and fake-import journey.
- [x] Remove README instructions to delete or rewrite uncertain import-state records; replace with safe reconciliation steps and state any missing recovery capability.
- [x] Correct current installation, test, fixture-isolation, and recovery guidance without rewriting unrelated history.

### Gate D analysis only

- [x] Map responsibilities, shared rules, repeated scans/projections, and real module coupling with file/function references.
- [x] Measure focused hot paths or label claims as estimates; distinguish display caching from mutation-time authorization checks.
- [x] Create `docs/maintenance-refactoring-roadmap.md` with ordered P0/P1/P2 test-backed milestones and compatibility risks.

## Final checks and delivery

- [x] Full offline unittest suite (419 tests, 1 capability skip), Python compilation, CLI help, JS syntax, Node tests, isolated broad browser run, and `git diff --check`.
- [ ] Clean checkout verification.
- [ ] CI workflow runtime validation (GitHub Actions run pending push).
- [x] Compare protected output hashes before/after.
- [ ] Commit test portability/CI, browser/docs, and roadmap as separate coherent commits where practical; push without rewriting history or merging PR #3.
