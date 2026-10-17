# P1 identity stability and reconciliation plan

1. Reproduce the suggestion-only conflict in an isolated `ReviewStore`; inspect registered candidate/event references and confirm the production output ledger is not present in this checkout.
2. Add a versioned authoritative-content comparator. Reuse a logical identity only for unique same-source matches, while keeping extraction IDs, candidate IDs, drafts, decisions, revisions, and approvals scoped to their original extraction.
3. Add append-only identity-link events and a read-only reconciliation projection for legacy held registrations. Require explicit confirmation, exclude ambiguous and import-protected cases, and make retries idempotent.
4. Surface a direct Resolve identity panel from the approval blocker, with authoritative-field differences, extraction metadata, proposal differences, protection and history status, individual actions, and a safe eligible batch count.
5. Add isolated unit/UI tests and synthetic performance/browser fixtures; document the identity contract and limits.
6. Run focused and full offline verification, compile and CLI checks, browser screenshots, performance measurement, inspect the diff, then commit and push the authorized feature-branch change.

## Safety decisions

- Candidate IDs remain physical review streams. A confirmed logical match adds a relationship; it does not alias IDs or transfer approvals.
- Source matching requires the same hash and exact conservative comparison of authoritative fields. Suggestions and extraction diagnostics are excluded.
- Existing held registrations are never changed automatically. Confirmation appends an event. Imported, uncertain, ambiguous, or unreadable histories stay protected.
- The production output path is read-only during this task; the configured checkout does not currently contain `output/review_events.jsonl`, so the reported 18 records cannot be enumerated here.
