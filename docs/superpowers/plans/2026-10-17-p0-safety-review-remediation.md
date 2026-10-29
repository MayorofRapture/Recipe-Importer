# P0 Safety and Review Correctness Remediation Plan

> Inline execution on the existing feature branch, with Gate A committed and verified before Gate B. No merge.

**Goal:** Prevent browser tests from touching production or unowned apps, prevent duplicate creates across validated identity links, and make source-unavailable approval and missing-instructions navigation exact and stable.

**Architecture:** Put fixture path, marker, attestation, and owned-process checks behind one browser test runner and one runtime validator. Use validated identity-group protection at the existing locked intent boundary only if reproductions show the candidate-only check allows a duplicate. Keep UI preview and durable approval bound to one persisted revision, and make the manual-instructions choice a one-time state transition.

**Tech Stack:** Python 3.14, unittest, Streamlit AppTest, Node.js, Chromium CDP, existing filesystem lock and JSONL event stores.

**Spec:** User attachment `P0 Safety and Review Correctness Remediation` (2026-10-17), Gates A and B.

## Global constraints

- Never modify production output, review/import ledgers, caches, or scans; browser writes use synthetic fixtures and fake providers only.
- Do not auto-reconcile identities, approve recipes, call Gemini/Mealie, merge PR #3, rewrite history, or touch `.vscode/`.
- Keep Gate A and Gate B in separate commits and push only after verification.
- No new dependency; preserve legacy identities/history and distinguish known rejection from uncertain create outcomes.

## Review focus

- A caller-supplied URL, forged banner, missing/invalid marker, stale server, or process not owned by the runner must be rejected before any UI action.
- Output/source paths, including ancestors, descendants, symlinks/junctions, and escaped sources, must not resolve into production data.
- Fixture mode with credentials must remain isolated and use synthetic candidates/fake services.
- Valid linked candidate versions must share create protection while distinct recipes from the same scan remain importable.
- Concurrent UI/CLI dispatch, malformed history, legacy aliases, and uncertain intents must fail closed under the existing lock.
- An approval preview, acknowledgment, and durable approval must name the same exact persisted revision and digest across reruns and sessions.
- Choosing manual instructions must navigate once, preserve the draft, and leave the editor usable.

## Gate A — Data safety

### A1: Runtime and browser isolation

- [x] Inspect every browser harness for write-capable actions and record its baseline hash in `.test-tmp/p0-remediation/source-baseline.txt`.
- [x] Add runtime tests for invalid fixture config, missing/invalid markers, output overlap with production, source escape/link traversal, and live-service construction in fixture mode.
- [x] Add shared-runner tests for forged/unowned/stale servers, absent/invalid attestation, missing candidates, and success with an owned synthetic server.
- [x] Add one shared fixture runner that prepares disposable data, launches Streamlit with absolute paths and fake services, attests its owned process/runtime configuration, supplies browser flows without accepting arbitrary URLs, and owns cleanup.
- [x] Route every write-capable harness through that runner; keep read-only diagnostics separate only if their interface cannot mutate state.
- [x] Verify each runner negative case before browser interaction; browser-tested only owned synthetic apps. The baseline/final hashes match for `review_events.jsonl` and `import_state.jsonl`; four ancillary JSONL files had last-write times predating this run and were not changed by the owned fixture workflows.

### A2: Linked identity create protection

- [x] Write synthetic reproductions for both extraction orderings and separately approved linked candidates; distinct recipes sharing a source remain independently eligible under the existing identity tests.
- [x] Test prepared plans, sequential and concurrent service instances, CLI/UI paths, legacy protected aliases, uncertain outcomes, durable intents, known rejection, malformed history, and ambiguous link holds through the focused and full suites.
- [x] Add one indexed group-aware protection projection and consult it under `_state_lock` immediately before each durable intent; preflight stays read-only and dispatch recomputes under lock.
- [x] Measure the projection against the same synthetic pair with and without the validated link: median 9,987 ns linked vs 10,750 ns unlinked across 7 x 20,000 calls. This microbenchmark is within noise and shows no material linked-case overhead.
- [x] Commit and push Gate A after final staged review (`c789b4e`).

## Gate B — Trustworthy review

### B1: Revision-bound manual approval

- [x] Add UI tests for unsaved field edits, changed persisted revision after acknowledgment, source hash mismatch, draft preservation, and legacy source-verified approval.
- [x] Display the persisted exact revision and all authoritative content, material warnings, and unresolved uncertainties before acknowledgment.
- [x] Bind acknowledgment/rationale to revision+digest; invalidate on buffer or persisted revision change. Approve only the already-saved revision; never save and approve a different buffer under an existing acknowledgment.
- [x] Exercise manual source-unavailable approval through the isolated browser; broader `browser_review_acceptance.mjs` still fails intermittently while switching from the default recipe to its proposal fixture, so that broader flow is not claimed verified.

### B2: One-time manual instructions navigation

- [x] Add AppTest coverage for stable render after manual entry, editor input, tab persistence, non-manual choices, and draft preservation.
- [x] Replace persistent-selection rerun with a manual-choice-only navigation callback that preserves the candidate and current draft without changing decision authority.
- [x] Exercise the browser workflow on the owned synthetic app and retain sanitized screenshots under `.test-tmp`.

- [x] Run focused/full tests, Python compilation, CLI help, JavaScript syntax checks, and diff checks.
- [x] Commit and push Gate B as the separately scoped follow-up commit.

## Integrated verification and delivery

- [x] Run isolation, linked import/concurrency, manual-approval, and instructions-navigation tests plus full offline unittest suite (416 tests passed, 1 skipped).
- [x] Run targeted Python compilation, CLI `--help`, JavaScript syntax checks, and `git diff --check`.
- [x] Compare production review/import ledger hashes before and after browser testing; both are byte-for-byte unchanged. Ancillary output ledger timestamps predate this run.
- [x] Report benchmark results, F2 reproduction outcome, browser/accessibility/live-service limits, commit SHAs, and PR #3 status after both commits are pushed.
