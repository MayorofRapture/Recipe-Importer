---
name: gemini-model-routing
description: Design and test configurable Gemini model lists for recipe extraction, stronger-model escalation, and quota-aware fallbacks. Use when changing Gemini model selection, 429/RPM/RPD/TPM handling, retries, usage counters, concurrency, or extraction cache versioning.
---

# Gemini Model Routing — Recipe-Importer

## Purpose

Keep a small, testable model router so the user can supply an ordered list of Gemini model IDs for two distinct decisions:

- **Quality escalation:** a different, stronger model may extract more detail from a hard-to-read scan; it **cannot recover information absent from a legible source**. The stronger model may produce **unverified suggestions** for missing content, never silently transcribe them as fact.
- **Quota fallback:** when a model is temporarily unavailable or rate-limited, move to another *eligible* configured model, if project/provider limits allow. A fallback model may be weaker; record why it was selected.

## Configuration design

Do not hard-code the current Gemini model catalog or presume every user-typed identifier exists. Validate IDs and capabilities (PDF/image input, structured output) against the configured provider or catch invalid-model errors cleanly. Prefer a compact, user-editable configuration: ordered model entries including `id`, `priority`, `role`, `rpm`, `rpd`, `tpm` when known, and whether enabled. Account/provider limits are authoritative; local settings are upper bounds, not guarantees.

Illustrative configuration (not guaranteed-valid IDs or release-specific quotas):

```json
{
  "models": [
    {"id": "MODEL_ID_PRIMARY", "role": "primary", "rpm": 10, "rpd": 20},
    {"id": "MODEL_ID_STRONGER", "role": "quality_escalation", "rpm": 5, "rpd": 20},
    {"id": "MODEL_ID_FALLBACK", "role": "quota_fallback", "rpm": 5, "rpd": 20}
  ]
}
```

Support selecting a model explicitly for a manual retry as well as automatic routing after implementation. Explain model/cost tradeoffs, and allow paid-vs-free API preferences to be configured.

## Router behavior

1. Examine on-disk extraction status, model attempted, cache version, and review decisions. Do not reprocess successful imports just to change models.
2. Select an eligible configured model based on task type, capability, project limits, and prior attempts. **Escalate because of reading/schema uncertainty**, not because an author omitted a quantity. For a genuinely absent value, preserve the diagnosis and optionally solicit a separate proposal using the appropriate configured model.
3. Rate-limit requests **globally and per model** across all worker threads. Account for requests that start but fail; do not exceed configured request-start or token budgets. Prevent concurrent over-issuance with a lock/central scheduler.
4. On 429/resource exhaustion, honor provider retry hints and distinguish per-minute vs per-day vs shared-project quota when possible. Persist a bounded usage ledger across restarts. Avoid looping between exhausted models; back off or stop cleanly with an actionable report.
5. Reset daily counters at the provider-defined boundary (confirm the relevant Google daily reset semantics, not the user's local midnight). External traffic against the same key/project can consume quota outside this script: local counters are best-effort only.
6. For 5xx/network timeouts, retry with bounded exponential backoff and jitter. Avoid hammering providers with long chains of five retries for a hard daily cap.
7. Cache extraction attempts with source hash **plus model and prompt/schema version**. Preserve all prior attempts. A rerun may reuse a compatible cache; an explicit retry must not accidentally serve stale cached output.
8. Write route decisions (model, attempt, reason, status, known quota evidence) to logs/results without leaking API keys or raw provider error bodies containing secrets.
9. Never let a new model's changed `recipes[]` ordering bypass `import-safety` identity reconciliation or human review approvals.

## Tests

- Primary model succeeds -> no unnecessary escalation.
- Missing-from-source but legible -> diagnosis and optionally suggestion; **no invented authoritative content** and no pointless endless escalation.
- Primary extraction/schema fails -> eligible quality model used.
- One model reaches RPM/RPD -> pause or route to eligible fallback according to configured provider/project limits.
- All models exhausted -> clean stop/report; previously imported recipes untouched.
- Restart -> persisted usage considered; same request is not accidentally reissued above quota.
- Mixed-model attempts for one multi-recipe file -> stable identities or manual reconciliation; no duplicate Mealie posts.
- Cached extract-only run and subsequent import do not spend new Gemini calls unnecessarily.

Mock Gemini, clock, and state store; never require actual quota consumption in tests. Consult current provider documentation before coding against quota headers or model identifiers.
