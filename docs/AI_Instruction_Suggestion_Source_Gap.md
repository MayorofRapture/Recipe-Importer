# AI instruction suggestions when the scan is unavailable

## Decision

Text-only instruction suggestions may be requested for a registered, editable recipe without a verified source scan. This request uses the existing durable suggestion job, model router, response schema, and review history. It sends only the recipe title, ingredients, current instructions, and registered instruction targets; it never sends source bytes.

The Review page keeps the action visible beside the instruction editor. Empty and whitespace-only instructions use **Suggest missing instructions with AI**. Existing instructions use **Suggest improvements with AI**. A fresh opt-in consent is required for each attempt. When the original scan is unavailable, the page labels the result as inferred and warns that the scan is still required for final approval.

## Safeguards retained

- ReviewStore verifies candidate registration, identity status, current revision, and import protection before recording a request.
- Missing title or usable ingredients prevent a request; missing explicit consent still prevents request creation.
- Generated proposals remain inferred and unverified, require explicit review, and use the existing target or insertion-location handling.
- Source availability remains an approval blocker; accepting a proposal does not verify the source or clear other findings.
- Gemini configuration is read through one helper that loads the project `.env` without overriding process environment settings or exposing the key.

## Verification notes

Automated offline verification uses synthetic local records and fake provider callbacks. No live Gemini or Mealie requests are part of the workflow.
