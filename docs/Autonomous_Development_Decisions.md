# Autonomous development decisions

## 2026-10-15 — Execution authority and existing work

- **Verified:** existing development branch has substantial uncommitted R1 implementation and design artifacts; all 118 baseline tests pass.
- **Decision:** continue in the existing checkout with exclusive file ownership, preserving this work; use the approved R1.2 contract and R1–R6 roadmap rather than replacing them.
- **Alternatives:** isolated worktree would omit uncommitted work; resetting would destroy it. Neither serves this handoff.
- **Reason:** protects prior work and avoids redundant implementation. Agents use configured Luna High defaults without overrides.
- **Tradeoff:** integration must explicitly coordinate shared contracts. Sol reviews diffs and task evidence.
- **Review later:** at each integration checkpoint.

## 2026-10-15 — Checkpoints and safety

- **Decision:** prior roadmap approval checkpoints are internal verification gates under the latest user authorization. Existing design documents supply the design; no repeated brainstorming approval loop.
- **Alternatives:** request approval after each milestone or treat old placeholder scope as current. Both contradict the handoff.
- **Reason:** permits continuous implementation while retaining exact-revision approval and production-data boundaries.
- **Tradeoff:** design assumptions are recorded and actual screenshots inspected; no invented user usability observations.
- **Review later:** final product acceptance; live service compatibility is separately reported.

## 2026-10-15 — Fixtures and remote effects

- **Decision:** automated and browser verification use temporary synthetic data and fake clients. Add an explicit fixture-mode guard before UI import/extraction clients exist.
- **Reason:** path isolation alone does not prevent accidental remote writes when credentials are present.
- **Tradeoff:** mocks establish local behavior, not deployed Gemini/Mealie compatibility.
- **Review later:** separately authorized live smoke verification.

## 2026-10-15 — Local verification environment

- **Verified:** default Windows temporary directories intermittently deny nested writes under the sandbox; tests pass when `TEMP`, `TMP` and `TMPDIR` point to ignored `.test-tmp` inside this checkout.
- **Decision:** isolate test fixtures there, retaining normal production paths. No security settings or existing temporary directories are altered.
- **Verified:** Playwright package acquisition is blocked by cache-only npm and unavailable pip packages; cached Chromium exists.
- **Decision:** investigate direct Chromium DevTools automation as the local browser fallback. Report its evidence separately from Playwright.
- **Review later:** browser setup agent's actual interaction/screenshot results.

## 2026-10-15 — Rejecting a completion does not resolve source uncertainty

- **Authority:** approved R1.2 requires rejected/deferred proposals to remain blocking; a proposal rejection alone cannot clear a critical source finding.
- **Decision:** retain that rule. A separate explicit evidence-backed resolution may confirm the current source value or record that an optional proposal is not needed. Preserve the original proposal and rejection history.
- **Alternative:** treating every rejection as permission to import would conceal genuinely missing critical information. Forcing acceptance of an invented optional value would violate source fidelity.
- **Reason:** the operator must be able to decline a completion without endorsing an incorrect recipe, and can subsequently resolve the actual source question.
- **Review later:** multi-proposal end-to-end approval and optional-omission UI tests.

## 2026-10-15 — Retry and reconciliation authority

- **Decision:** intentional re-extraction runs through the existing `BatchStore` and `JobService` adapters in a unique `output/reextraction_attempts/<attempt_id>` directory. It does not write to the active extraction cache or publish new candidates by default.
- **Identity:** retry comparisons report exact-content and title matches as suggestions only. Position is never identity; duplicate titles and ambiguous candidate identities remain held for an explicit operator choice.
- **Mapping:** same-source section mapping creates a fresh review revision, preserves unselected current draft fields, and requires approval again. Changed source bytes block mapping onto an old identity. A declared-new event is append-only, requires source bytes to still match the attempt, and exposes only that candidate for review from its isolated cache; it does not copy the cache or disturb sibling candidates.
- **Protection:** candidates with imported, uncertain, or in-flight import intent are excluded from retry mapping and approval. Malformed protection or retry history fails closed. Defer and cancel preserve active drafts and extraction data.
- **Runtime boundary:** normal local and explicitly consented cloud attempts use configured `JobService` runtime adapters without requiring an injected extractor. Tests inject deterministic extractors; fixture mode blocks live adapters. No live Gemini request was made.
- **Verification:** focused review-authority, reconciliation, import-service, history, and catalog suites passed (61 tests); `py_compile` and `git diff --check` passed. Browser/UI integration and live service compatibility remain separate checks.
- **Review later:** validate UI recovery/display behavior and the complete integrated suite after concurrent UI changes settle.

## 2026-10-15 — Review workspace and launch path

- **Decision:** keep Upload, Review, and Import & history as distinct destinations. Review uses a compact source/editor workspace, explicit draft save, exact-revision approval, and explicit import preflight; these transitions remain separate.
- **Decision:** use Streamlit-native row menus, form controls, paginated queue disclosure, and bounded source page/zoom controls. Keep complete Issues and retry comparison available without permanently consuming editor width.
- **Decision:** fixture mode requires an explicit isolated output folder and `.fixture-library` marker before catalog reads. Preview paths are restricted to marked synthetic roots; normal mode retains the configured local library.
- **Reason:** preserve review authority and source provenance while fitting the scan and editor in the first viewport. Avoid production data in browser fixtures and avoid adding custom interaction code where native widgets suffice.
- **Tradeoff:** browser screenshots and direct navigation/reload recovery remain final verification gates; AppTest establishes widget/state behavior but not rendered accessibility or layout.
- **Verification:** 53/54 scoped Streamlit/UI regressions currently pass; the remaining test asserts same-title sibling import behavior and awaits the concurrent identity-protection fix. No live Gemini or Mealie calls were made.

## 2026-10-15 — Cross-process quotas and protected retry identities

- **Decision:** CLI and UI extraction share an atomic local Gemini usage ledger. Daily request reservations and persisted global/per-model request-start windows are reread and saved under a short cross-process file lock; the strictest configured interval seen that UTC day is retained. Rate waits and provider calls occur after releasing that lock.
- **Limit:** local counters see only workers using this same output ledger. They cannot account for other applications or provider-project traffic, UTC midnight is only a local daily reset proxy, and configured TPM is not enforced because routing has no input-token estimate or observed-token reservation interface. Provider quota remains authoritative; do not claim a model switch avoids shared account limits.
- **Decision:** an unmatched retry candidate cannot be declared new when its source contains a protected imported/uncertain sibling. The current source model has no independent recipe-boundary anchor, so these cases remain held with map/defer guidance until such evidence can be represented and checked.
- **Verification:** deterministic fake-clock shared-window/restart and spawned-process daily-limit tests pass; rename/reorder import and protected-source declaration regressions pass. No live Gemini or Mealie calls were made.

## 2026-10-16 — Review first-viewport compaction

- **Decision:** place Queue search/filter/jump controls in a popover sharing the Review title/status row; keep View mode and Previous inside the existing Options action popover; use one bounded page/zoom row for the source scan and put its human filename beside the scan label.
- **Decision:** collapse optional yield/description fields by default. Keep the Review heading semantic and compact.
- **Tradeoff:** Streamlit's measured default top padding leaves excessive space before this workflow, so Review applies a page-local `stMainBlockContainer` padding and vertical-gap override. It does not restyle widgets or alter Upload/Import pages; text may scroll vertically at enlarged sizes.
- **Reason:** move source evidence and recipe fields higher while preserving exact queue search, save/approval boundaries, source page limits, and readable controls at enlarged text sizes.
- **Verification:** 26 focused AppTest review/navigation/reconciliation regressions pass. Browser measured pane borders at y239 and confirmed H1 Review; source text and ingredients appear at y380/y415 with instructions at y682 in 1440x900. No horizontal overflow at 1440, 320, or 200% root text size; enlarged text scrolls vertically.

## 2026-10-16 — Multi-source catalog read performance

- **Measured:** 100 synthetic source files with 1,000 prepopulated candidates took 5.990s warm p50 / 6.370s p95 over three samples. After exact registered-identity reuse, one-time decision projection, removal of per-candidate source-map copies, and per-load source-hash reuse shared with source resolution, warm p50/p95 measured 0.410/0.796s. External exact-revision approval remained visible after reload (0.335s).
- **Decision:** keep catalog optimization local to one load and only reuse immutable exact registrations from that loaded snapshot. Missing, duplicate, or uncertain registration matches still use the locked refresh path. Approval/import mutations remain live locked reads; source bytes are hashed again on the next catalog load and at approval/import boundaries.
- **Limit:** synthetic prepopulated warm data is not cold registration or full Streamlit rendering, and three samples do not establish hardware-independent latency. See `wireframes/ui-redesign/evidence/catalog-multisource-100x1000.json`; no persistent read cache or whole-UI performance claim.
- **Verification:** 39 focused review-authority/catalog tests pass, including shared-source single-hash, external registration, approval, import-intent and changed-source refresh cases. No live service calls were made.
