# Manual Approval and Review Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Let reviewers safely handle lost-source recipes and intentional no-instruction recipes while blocker actions retain the selected candidate and imports keep exact approval provenance.

**Architecture:** Keep source-verified approval as the default. Add append-only, exact-revision human decisions for source-unavailable approval and intentional absence of instructions; project these through the shared effective review and import-eligibility paths. Fix blocker routing at the selected-candidate/editor state boundary and make browser verification own a temporary, marked fixture process.

**Tech Stack:** Python 3.14, Streamlit 1.66, unittest/AppTest, local Mealie client fakes, Node Chromium/CDP harness.

**Spec:** User attachment `P1 Manual Approval, Missing Instructions & Review Navigation` (2026-10-18).

## Global Constraints

- Preserve `output/` and its append-only review/import ledgers; tests must use a unique isolated marked fixture directory and assert the production-output digest is unchanged.
- Never relabel missing-source approval as source-verified or change the registered source hash.
- Never infer “no instructions required”; require a durable, revision-bound human decision.
- Do not bypass identity, suggestions, critical findings, protected import outcomes, or duplicate-import safeguards.
- Do not make live Gemini or Mealie calls and do not merge PR #3.
- Keep legacy source-verified approvals valid without rewriting events.

## Review Focus

- A missing or reconnected source whose bytes do not match must remain unverified and blocked unless the reviewer chooses manual approval.
- A changed revision must invalidate a no-instructions decision and approval tied to its previous digest.
- Existing source-verified approvals and already imported or uncertain recipes must retain current protections.
- A blocker action that changes filters or tabs must not let Streamlit silently select another candidate or lose its draft.
- Malformed or conflicting new authority events must fail closed without invalidating valid legacy source-verified approval.

## Files and Responsibilities

- `app_pages/review.py`, `suggestion_ui.py`: blocker destination, section/finding focus, source recovery and explicit review controls.
- `review_store.py`, `recipe_quality.py`: versioned durable decisions, exact revision projection, completeness rules, source authority.
- `ui_data.py`, `ui_import_service.py`, `import_recipes.py`: shared eligibility and GUI/CLI import compatibility.
- `app_pages/import_history.py`: inspect approval method and provenance in preflight/history.
- `tools/browser_identity_acceptance.mjs`, `runtime_safety.py`, `tests/`: isolated browser-owned fixtures and regressions.
- `README.md`, `docs/`: supported behavior, Mealie empty-instruction uncertainty, validation evidence.

---

### Task 1: Preserve candidate selection and route blocker actions

**Interfaces:** Keep existing `ApprovalBlocker.anchor` values as the stable blocker contract; map them to editor section, source recovery, issue, suggestion, or identity state in `review.py`.

- [x] Write AppTest regressions for source-unavailable, missing-instructions, title, ingredient, suggestion, finding, identity, and protected-outcome destinations; assert candidate ID and current draft survive reruns and filter membership changes.
- [x] Run focused tests and confirm failure before code changes.
- [x] Implement consumption of `review_jump_to` and pending per-candidate destination state before editor widgets render; preserve candidate when outside filtered/page membership and provide a return route.
- [x] Verify Back, Skip, filters, pagination, and unsaved buffers with existing tests.

### Task 2: Persist intentional no-instruction decisions

**Interface:** Add a `no_instructions_decision(candidate_id, source_hash, extraction_id, revision, content_sha256)` projection from immutable decision events; no decision means instructions remain required.

- [x] Add failing ReviewStore tests for missing instructions, explicit reason, restart persistence, stale revision/digest rejection, and later added instructions.
- [x] Implement append-only decision events scoped to exact candidate/source/extraction/revision/content digest.
- [x] Add `is_complete(..., no_instructions_confirmed=False)` behavior without weakening other callers; projection passes confirmation only for the exact current revision.
- [x] Add Review UI choices for manual text entry, existing AI request path, explicit no-instructions confirmation with reason, and keep-incomplete; preserve the empty list in `schema_recipe`.

### Task 3: Add source-unavailable manual approval authority

**Interface:** `approve_revision` accepts explicit `approval_method`, acknowledgment, and rationale. Default remains `source_verified`; `manual_source_unavailable` may waive only file availability/hash verification and writes a versioned immutable approval event.

- [x] Add failing tests for both methods, missing/mismatched source, required acknowledgment, rationale, digest/revision mismatch, pending suggestions/findings, unresolved identity, and protected imports.
- [x] Implement one shared approval-policy projection that verifies exact approval event, method, source provenance, and current blockers; retain legacy events as `source_verified`.
- [x] Add Review controls to restore the original file by hash or explicitly review and approve without it; show the unverified state and require confirmation before manual approval.
- [x] Expose approval method/provenance in effective review and review history without changing source hash.

### Task 4: Unify import eligibility and preserve provenance

**Interface:** `ImportService._eligibility()` consumes the shared effective approval authority; valid manual approval can pass source availability only, while identity, protected outcome, exact revision, and import history checks remain mandatory.

- [x] Add failing plan/dispatch tests for valid manual approval, forged/stale method, altered digest, unresolved identity, pending issue, protected outcome, and source-verified compatibility.
- [x] Remove independent raw registration identity/source gates where they contradict the shared authority; retain exact candidate and protection checks.
- [x] Record method, rationale, original source hash, revision, and digest in durable local import intent/history and make preflight display the provenance.
- [x] Verify `schema_recipe` emits an empty `recipeInstructions` list and mock Mealie client success/rejection paths; do not retry an uncertain create. Document endpoint behavior as unverified live if mocks cannot establish server acceptance.

### Task 5: Isolated browser verification, performance, docs, and closure

- [x] Add a failing isolation assertion/test for browser harness environment paths, fixture marker/candidate count, and production-output digest before and after.
- [x] Make the harness create a unique temp library, seed sanitized synthetic records, launch and own the Streamlit process with explicit absolute fixture paths, and refuse arbitrary existing servers.
- [x] Capture the six requested states and run happy path plus recovery path with no access to production scans or ledgers.
- [x] Benchmark review navigation and import preflight over 1,000 synthetic recipes; retain indexed lookups and report measured results.
- [x] Update docs, run the full offline suite, compileall, CLI help, JS syntax, diff check, production-output integrity comparison, and focused AppTest/browser checks.
- [x] Commit and push to `feature/complete-recipe-workspace`; update PR #3; do not merge.

## Plan Review

All four user phases are represented. The task interfaces preserve exact-revision authority across review and import; the main known uncertainty is Mealie v3 acceptance of an empty instruction list, which stays explicitly unverified absent a version-matched contract or live-write authorization.


## Completion evidence

- Full offline suite: `.venv\Scripts\python.exe -m unittest discover -s tests` — 404 tests passed in 229.423 seconds.
- `compileall`, `import_recipes.py --help`, `node --check tools/browser_identity_acceptance.mjs`, and `git diff --check` exited successfully.
- Isolated Chromium screenshots are under `.superpowers/sdd/2026-10-18-manual-review-missing-instructions-navigation/screenshots/` (six requested states). The run used a unique marked synthetic library and an owned Streamlit process.
- 1,000 synthetic candidates / 700 history rows: catalog load median 407.14 ms, p95 487.36 ms; queue-page projection median 0.03 ms, p95 0.12 ms; all-candidate import preflight 710.18 ms (1 eligible, 999 excluded).
- Production `output/` remained unchanged: 464 files before and after, with zero added, removed, or changed files.
- Mealie acceptance of an empty `recipeInstructions` list remains unverified for the installed server. The default-off guard remains in place; no live Mealie or Gemini calls were made.
- Implementation commit `c4c6f1b515951faac2b90a6f3cbf78d117d89b84` was pushed to `feature/complete-recipe-workspace`; this plan close-out follows in a separate commit. PR #3 remains open and unmerged.
