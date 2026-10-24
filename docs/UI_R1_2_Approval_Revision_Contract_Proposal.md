# R1.2 review authority and exact-revision approval contract

**Status: approved for R1.2 implementation with the mandatory amendments below.** This contract governs local review state only. R1.2 does not implement R2 visual work, R1.3, or a Mealie import-intent dispatcher.

## Authority and compatibility

`output/review_events.jsonl` is the sole authoritative store for all new candidate registrations, review revisions, issue resolutions, and approvals. Existing `ui_drafts.jsonl` records remain readable as legacy drafts; R1.2 does not write new review revisions there. Historical extraction caches, review events, drafts, and import state are append-only/preserved, with no silent migration or rewrite. Legacy decision and draft behavior is retained only when identity and chronology are unambiguous. Ambiguous records are preserved and protected pending explicit reconciliation.

The UI and CLI must use the same effective-review projection and eligibility calculation. The import payload comes from that projection's exact approved effective recipe: the durable revision snapshot plus only issue resolutions named by the matching approval. `schema_recipe()` of the CLI candidate must equal `schema_recipe()` of that effective approved recipe. Raw, unapplied bulk text never enters the payload. Legacy complete candidates retain their pre-R1.2 CLI behavior when there is no modern revision/approval stream; a modern candidate may be imported only from a matching exact-revision approval.

## Candidate registration and identity

Every candidate in a source/extraction is explicitly registered before review, whether through the UI catalog or CLI extraction path. Registration persists an opaque UUID `candidate_id`, source digest, immutable `extraction_id`, candidate fingerprint used only as a matching hint, evidence anchor when available, and identity status. Re-reading the same extraction reuses a registration only when exactly one registration matches. Candidate title and array position are never identity. New title duplicates with distinct evidence/fingerprints receive distinct UUIDs. Indistinguishable siblings without source-location evidence remain protected as `needs_reconciliation`.

Legacy IDs (`source_hash:normalized_title_digest`) are retained where a unique legacy match exists. A duplicate-title legacy identity or import record is never reassigned to a sibling. Both siblings remain protected until an explicit reconciliation associates the historical record.

## Immutable issue and row targets

Registration also records immutable target bindings for source rows, diagnostics, and proposals. A binding includes a stable `target_id`, extraction lineage, target kind, original source field/value, and immutable evidence reference or unique source-content anchor. Mutable paths such as `ingredients[2]` may be retained as display metadata, but they are not authority for a decision. If the original target cannot be mapped uniquely, the issue stays unresolved and the operator must reconcile it. Reordering/removing rows does not retarget a proposal or issue to whichever item later occupies the same index.

## Additive event contract

Each new event is append-only JSONL with `schema_version`, stable `event_id`, caller-supplied `operation_id` for retry idempotency, `candidate_id`, `source_hash`, `extraction_id`, UTC timestamp, and actor. Registration, revision, issue-resolution, and approval event kinds are additive; legacy event kinds remain readable.

### `candidate_registration`

Stores the opaque candidate UUID, immutable extraction ID, source digest, canonical source-derived fingerprint/matching hint, evidence anchor, identity status, and immutable target bindings. A unique registration is reused; ambiguous exact siblings are registered as protected and cannot be approved/imported. Registration never rewrites legacy records.

### `review_revision`

Stores one complete durable recipe snapshot, monotonically increasing `revision`, `parent_revision`, immutable row/target bindings, applied edits, raw bulk text/mode/warnings as review evidence, and `content_sha256` over a versioned canonical encoding of the import-relevant recipe fields. Raw, unapplied bulk text is review evidence, not recipe content. Save requires the expected parent revision to still be current. Repeating the same `operation_id` returns the already-written revision after readback instead of appending another revision.

### `review_issue_resolution`

Resolves exactly one immutable `issue_id` and `target_id`, with disposition, original proposal/finding, optional final value, and the exact revision/extraction to which the decision applies. The decision is never indexed by current list position. A proposal value enters the authoritative recipe only through an explicit accepted/edited resolution bound to its immutable target. Rejected/deferred proposals and unresolved critical findings remain blocking; deleting a row does not resolve an issue.

### `review_approval`

Attests to one exact candidate, source digest, extraction, revision number, content digest, the set of required issue-resolution event IDs, and a canonical digest of those immutable resolution contents. Approval requires a durable revision readback, complete recipe, no unresolved critical issue or pending proposal, a uniquely registered identity, an available source scan whose bytes match `source_hash`, and no protected import outcome. A same-name source relink is accepted only after digest verification. A later revision immediately ceases to be import-ready while the prior approval remains in history. A newer legacy draft invalidates modern approval until a fresh revision is saved and approved; an unchanged previous approval remains available when the source scan is unavailable, but no new approval can be recorded. Repeating an approval operation ID returns the original approval only if all bound values still match; otherwise it returns a conflict.

## Idempotency, locking, concurrency, and later import intents

Save, issue-resolution, and approval calls require stable operation IDs. Under a shared cross-process Windows-compatible OS file lock, each operation re-reads current state, checks the operation ID, validates its expected revision/parent and binding, appends and fsyncs one event, then reads it back by event ID and verifies its digest. A retry after an ambiguous write/readback returns the matching durable event; a mismatched reuse of an operation ID is a conflict. JSONL is never rewritten to recover a partial write; damaged trailing data is reported for explicit recovery.

The lock file is shared by UI and CLI and protects only short local read/validate/append/readback transitions. It is never held while calling Mealie. R3 must add a durable unique per-candidate import intent (operation UUID, candidate ID, exact approved revision/digest, state, and eventual slug/outcome) under this lock before making a network request, then release the lock. A second process must see the existing intent and reconcile it instead of issuing a second create. An interrupted or ambiguous remote outcome remains protected until reconciliation. R1.2 designs this boundary but does not implement intent dispatch or change Mealie calls.

No reliable version gate exists for old CLI processes. Therefore the R1.2 CLI must share the effective projection before the UI exposes modern approvals; concurrently running an older binary is unsupported and must be documented. The implementation must not silently treat unknown modern events as legacy approval.

## Legacy compatibility

- Legacy review decisions continue to apply when their candidate identity is unique and their proposal target can be mapped uniquely to the registered extraction target.
- Legacy whole-candidate decisions remain effective only when no later, non-equivalent legacy draft exists and chronology is provable.
- Legacy `ui_drafts.jsonl` is read-only input. A legacy draft after approval invalidates that approval. If ordering/equivalence cannot be established, show needs review and require fresh approval.
- Legacy import records remain protected. Duplicate-title matches are not assigned by array index or title alone; explicit reconciliation is required.
- No historical file is rewritten or deleted by registration or projection.

## Required R1.2 tests

- All R1.1 regressions pass, including duplicate-title identity/import protection, draft reload, stale approval invalidation, stable UI edits, and fake UI-to-CLI payload equivalence.
- Registration is opaque, repeatable for a uniquely matched extraction, preserves unique legacy IDs, and holds indistinguishable siblings.
- Proposal/issue decisions remain attached to immutable targets across reorder, deletion, and extraction changes; unresolved mappings block approval.
- Save and approval retries after durable append/readback ambiguity are idempotent; mismatched operation reuse and stale parent revisions conflict.
- Independent processes contend on the shared OS lock and cannot both commit from the same parent revision.
- UI and CLI project the same approved recipe and exact payload; incomplete/pending/empty recipes are rejected.
- New approval requires a hash-matching source; a newer legacy draft blocks prior consent while preserving the old event.
- Tests use temporary records and fake clients only; no live Gemini or Mealie calls.

## R3 import intent shape (design only)

Future intent records are uniquely keyed by an operation UUID and candidate/revision/content digest, and transition through `prepared`, `request_started`, `confirmed`, `definite_failure`, or `uncertain`. Creation is permitted only after an atomic local claim. Network I/O occurs outside the shared lock. Recovery must reconcile `request_started`/`uncertain` with Mealie before another POST; confirmed slug and tag progress are separate. This is a design boundary, not R1.2 implementation.
