# Gate D maintenance and refactoring roadmap

**Scope:** analysis and sequenced proposals only. No broad module extraction was
performed in Gate C. Preserve append-only JSONL compatibility, current IDs,
exact-revision approval, import protection, and Windows file locking in every
future milestone.

## Evidence snapshot

Measured on Windows 11 build 26200, Python 3.14.7, with the synthetic
`tools/benchmark_catalog.py` harness (1,000 candidates across 100 sources,
three warm repetitions):

| Operation | Measured result | Interpretation |
| --- | ---: | --- |
| Cold catalog load plus durable registration of 1,000 candidates | 2.037 s | Includes fixture creation and append-only candidate registrations; not a production-output measurement. |
| Warm catalog load p50 / p95 | 0.290 / 0.305 s | Three samples; too few to define a stable service-level target. |
| Approval appended externally, then visible after catalog reload | 0.282 s | Confirms external review updates become visible; includes a reload, not the complete approval action. |

The suite baseline was 416 passing tests and one skipped Windows directory
symlink test. The skip was caused by the host denying symlink privilege; the
junction-path regression passed. The baseline suite took 357.274 seconds.
These are measurements of this development host, not CI or user-machine
benchmarks.

Current module sizes were counted after the Gate C changes:

| Module | Lines | Main responsibilities |
| --- | ---: | --- |
| `review_store.py` | 2,661 | JSONL read/append/locking, registrations, identity authority, review revisions, issue resolutions, approvals, projections, legacy decisions. |
| `app_pages/review.py` | 2,141 | Queue/session transitions, source navigation, structured editor, proposal and finding actions, approval controls, page rendering. |
| `import_recipes.py` | 1,559 | CLI parsing/orchestration, extraction/retry, reports, legacy state, review command path and import dispatch. |
| `ui_import_service.py` | 908 | UI preflight and dispatch, fresh import protection, event outcomes, tag recovery, CLI candidate dispatch. |

The line counts identify review targets; they are not evidence that a module
must be split.

## Responsibility and authority map

| Concern | Current owner and entry points | Boundary to preserve |
| --- | --- | --- |
| Append-only review history and cross-process writes | `review_store.py::_reload`, `_append`, `_state_lock`, `ReviewStore.register_candidates`, `resolve_issue`, `approve_revision` | JSONL records remain readable and additive. Mutation re-reads and validates state under the Windows-compatible lock. Never cache authorization across a mutation. |
| Candidate identity and reconciliation | `review_store.py::register_candidates`, `_effective_identity_authority`, `identity_link_status`; `import_protection.py` | Identity links do not transfer approval. Ambiguous siblings and protected import outcomes remain held. |
| Effective recipe and approval projection | `ReviewStore.project_loaded`, `ui_data.load_catalog`, `review_store.apply_review_decision` | One exact source/extraction/revision/content projection determines what the UI and import services can use. Legacy projection remains separately compatible. |
| UI queue state and rendering | `app_pages/review.py::main`, `_apply_pending_queue_change`, `_render_queue`, `_render_editor`, `_render_suggestions` | Flush a candidate before changing selection; preserve unsaved buffers and candidate-bound widget IDs. Render actions still call durable services. |
| Import planning and dispatch | `ui_import_service.py::preview`, `dispatch`, `dispatch_cli_candidate`; `import_protection.py` | Preview is advisory. Dispatch re-reads current review and import state under lock, claims an intent, and makes the remote call outside that lock. Create and tag outcomes stay separate. |
| CLI extraction and reporting | `import_recipes.py::main` and its worker/retry/report helpers | Reuse shared candidate identity, review, and import boundaries; preserve existing CLI flags and report meanings. |

### Confirmed coupling and duplication

- `ui_data.py` imports `import_recipes` for `StateStore`, identity, and digest
  helpers, and imports review and protection modules for catalog projection.
  `ui_import_service.py` also imports `StateStore` and `file_digest` from the
  CLI module. This means the current CLI module is also a shared library
  dependency; splitting it requires moving stable primitives without changing
  serialized IDs.
- `review_store.py` houses storage, validation, identity, review authority, and
  projection. The call sites are distinct, but the state is intentionally
  reloaded under one lock for writes. A storage extraction must not make UI and
  CLI use separate ledgers or weaker consistency checks.
- `app_pages/review.py` mixes Streamlit rendering with action orchestration.
  For example, `_render_suggestions` renders a card and then dispatches an
  issue action; `_apply_proposal` edits a buffer before `_resolve_suggestion`
  records the durable decision. Separate pure state transitions from widgets
  only where a regression test can prove identical Streamlit behavior.
- Import protection is intentionally checked again for each planned recipe in
  `ui_import_service.py::dispatch` (`review._reload`, `_read_events`, and
  `_protection_snapshot` inside the candidate loop). This repeats ledger reads
  for a multi-recipe batch, but makes each create claim observe outcomes from
  earlier recipes and concurrent workers. Treat replacing this with a shared
  snapshot as a correctness-sensitive design question, not a safe cache win.
- Shared business rules are already partly centralized: GUI dispatch and CLI
  candidate dispatch both use `ImportService`, while source/extraction/revision
  authority lives in `ReviewStore`. No evidence supports merging legacy
  candidate decisions with modern review approval; they have different
  compatibility contracts.
- There is no measured evidence that project skills, fixture evidence, or
  historical planning documents are unused. Do not remove them as cleanup in a
  code extraction milestone.

## Recommended boundaries

Keep the initial target to a small number of modules:

1. **`review_events.py` (P1):** event line parsing, schema validation,
   operation-id checks, file locking, append/readback. Keep `ReviewStore` as a
   facade at first so existing imports and call sites remain stable.
2. **`recipe_identity.py` (P1):** logical digest, matching hints, identity
   links, and reconciliation eligibility. It must call the event facade for
   writes and must not own import history.
3. **`review_authority.py` (P1):** exact revision/content/source approval
   checks and shared effective projection, extracted only after the tests
   establish which rules are common to CLI and UI. Keep legacy review decisions
   behind their current compatibility path.
4. **`app_pages/review.py` (P2):** first isolate queue transitions as pure
   functions in `review_navigation.py`; consider editor rendering extraction
   only after that change reduces state coupling. Do not split every renderer
   into a separate file.
5. **Import performance (P2):** measure large histories and batches before
   changing ledger reads. Any read cache is display-only; dispatch must keep a
   fresh mutation-time protection check.

Do not introduce SQLite during these milestones. Retain JSONL as the durable
compatibility format; a future SQLite evaluation needs a separate migration,
backup, replay, and rollback proposal.

## Ordered milestones

### P0 — Lock down behavior before extraction

- Add table-driven contract tests for event validation, operation idempotency,
  revision conflicts, identity links, exact approval bindings, protected import
  outcomes, and CLI/UI import payload equality.
- Include legacy JSONL records and malformed/truncated history fixtures. Assert
  existing history is unchanged after read-only catalog and import preview.
- Capture append-only event shapes and Windows multiprocess locking behavior.
- Gate: no module moves until tests pass against the current implementation;
  no live Mealie/Gemini calls.

### P1 — Separate event storage behind a compatible facade

- Extract `_reload`, line validation, append/readback, and locking helpers from
  `review_store.py` to `review_events.py` while retaining `ReviewStore` methods
  and imports as delegating facades.
- Depend on the facade before moving identity or approval logic. Keep the same
  lock file path, event ordering, operation IDs, schema versions, and fsync /
  readback behavior.
- Validate against all historical fixture variants and run the complete
  Windows suite before changing any caller.
- Expected benefit is narrower persistence ownership and easier direct review;
  runtime improvement is unmeasured and not a goal for this step.

### P1 — Isolate identity reconciliation after storage stabilizes

- Move pure digest/match decisions first; retain append-only identity-link
  writes through `ReviewStore`/`review_events.py`.
- Keep source hash, extraction identity, content digest, candidate ID, and
  protected-import checks explicit in function arguments.
- Preserve behavior for duplicate titles, reordered/split recipe arrays,
  legacy IDs, imported siblings, uncertain outcomes, and prior approvals.
- Gate: run identity/reconciliation tests, multi-process tests, and the full
  offline suite; compare projection output before and after for the same event
  fixture.

### P1 — Establish one explicit modern approval projection

- Inventory `ReviewStore.project_loaded`, `ui_data.load_catalog`, CLI effective
  review, and `ImportService._effective` call paths. Extract only duplicated
  modern exact-revision rules into `review_authority.py`.
- Preserve legacy review decisions and drafts in a compatibility adapter.
  Never let pending proposals or raw drafts into the Mealie payload.
- Gate: verify source hash, extraction, revision, content digest, issue
  resolution digest, manual approval provenance, and protection status in both
  UI preview and CLI dispatch tests.

### P2 — Reduce Streamlit navigation coupling

- Extract pure queue transition decisions from `app_pages/review.py` into a
  small `review_navigation.py`; leave Streamlit widgets in the page module.
- Test jump, back/skip, filter changes, dirty-buffer flush, browser refresh,
  blocker jumps, and queue page boundaries through AppTest and the isolated
  browser fixture.
- Consider an editor-rendering module only if navigation extraction leaves a
  clear interface and reduces session-state dependencies. Avoid extracting
  presentation helpers without a caller or testability benefit.

### P2 — Re-measure history and batch costs before optimizing

- Extend `tools/benchmark_catalog.py` to report candidate/source counts, review
  event bytes, import history bytes, cold/warm catalog timings, preflight
  timing, and per-candidate dispatch validation cost, all on synthetic data.
- Add 1,000 and 10,000 candidate/history scenarios and at least 20 warm samples
  before using p95 to set a target. Record Python, OS, hardware, and method.
- Profile ledger parsing first. If it dominates, design a read-only index with
  file identity/size/mtime invalidation; write and dispatch paths must still
  reload authoritative state under lock. Compare correctness fixtures and
  complete suite before/after.
- Current measured warm catalog p95 is 0.305 s at 1,000 candidates on this
  host. This does not establish a user-visible bottleneck; no optimization is
  justified by the available three-sample run.

## Compatibility and risk register

- **Historical JSONL:** no rewrite, compaction, destructive migration, event
  renaming, or candidate-ID regeneration. Readers must continue accepting
  supported legacy event forms.
- **Approval:** identity linking never copies approval. Approval remains bound
  to exact source/extraction/revision/content and resolution digests.
- **Import protection:** uncertain or confirmed creates remain protected.
  Dispatch performs the current authoritative check immediately before intent
  creation; no warm catalog projection can authorize a POST.
- **Windows concurrency:** preserve shared file locks and test process-level
  contention. Do not replace them with thread-only locks.
- **Source provenance:** keep scans, caches, drafts, and extraction attempts
  separate from review events and preserve source hashes.
- **Failure recovery:** malformed history stays blocked for explicit repair;
  extraction must not silently rewrite damaged trailing JSONL.
- **Performance claims:** current numbers are synthetic benchmark results, not
  production workloads. Hypotheses above must be measured before implementation.
