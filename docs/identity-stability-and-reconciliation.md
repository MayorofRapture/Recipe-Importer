# Recipe identity stability and reconciliation

## Identity contract

Logical recipe identity uses the versioned `logical-recipe-v1` digest over the authoritative fields `name`, `ingredients`, `instructions`, `yield_text`, `description`, `notes`, and supported time, category, and tag fields. Ingredient and instruction wording and order are preserved exactly. Missing optional text is equivalent to its default empty value; missing lists are equivalent to empty lists. A changed title, quantity, instruction, or other listed field is a content difference. A title alone never establishes identity.

Suggestions, proposal dispositions, diagnostics, warnings, confidence, uncertainty, extraction method, source-line references, and other extraction metadata do not affect logical identity. Full extraction fingerprints and extraction IDs continue to include the extraction payload, so metadata changes still create distinct extraction versions.

## Candidate and history safety

Candidate IDs remain separate review streams. When a new extraction uniquely matches one registered candidate from the same source hash, registration creates a new candidate ID and an append-only `candidate_identity_link` event. This makes the identity warning unnecessary without sharing drafts, revisions, proposal decisions, or approvals. An approval remains bound to its exact candidate, extraction, revision, source hash, and content digest.

Automatic links require one authoritative match in the current extraction, one unambiguous prior logical group, the same source hash, and no protected import history. Duplicate siblings, changed recipe content, ambiguous legacy history, changed source bytes, and imported or uncertain source outcomes remain held. A user can select an exact-content match from the same source only after providing evidence; this creates a separate audited confirmation event. It cannot override import protection.

Older registrations do not need migration. The read-only reconciliation projection computes the versioned digest from each stored candidate when the digest is absent. A user confirmation appends a relationship event and leaves all prior JSONL registrations, fingerprints, revisions, decisions, and approvals untouched. Confirmation is idempotent across retries and application restarts. Deferral is also recorded append-only.

## Effective identity authorization

Review operations resolve identity from the exact candidate ID, source hash, and extraction ID. A held registration is treated as registered only when one durable link points to a registered prior candidate for the same source and authoritative-content digest. Missing, conflicting, malformed, or cross-extraction links remain blocked. This check runs against a freshly reloaded ledger under the mutation lock.

Identity links do not transfer approvals. Approval still binds the current candidate and extraction to its exact saved revision, content digest, verified source bytes, and required issue and suggestion decisions. Protected or uncertain Mealie outcomes continue to block new approvals and generated-suggestion operations independently of identity status.

## Review behavior and limits

The Resolve identity panel shows the candidate, source reference, extraction versions, authoritative differences, suggestion and diagnostic differences, review activity, and source import protection. It offers batch confirmation only for the exact count of independently eligible matches. Protected or ambiguous records are excluded. After linking, all remaining source, quality, and proposal blockers continue to apply.

The logical comparison is intentionally exact for authoritative recipe wording; it may leave harmless formatting changes for human reconciliation. The application cannot establish that a source hash still corresponds to available original scan bytes without the source file. No automatic action rewrites the existing review ledger, and no live Gemini or Mealie call is involved.
