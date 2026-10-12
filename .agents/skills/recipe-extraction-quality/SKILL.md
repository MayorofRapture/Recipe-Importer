---
name: recipe-extraction-quality
description: Diagnose incomplete recipe scans and improve Gemini recipe extraction. Use for missing or unreadable ingredient quantities, directions, cooking time/temperature, multi-recipe JPG/PDFs, review reports, and Gemini-proposed completions requiring human approval.
---

# Recipe Extraction Quality — Recipe-Importer

## When to use

Use when changing Gemini prompts, structured extraction schemas, recipe validation, quality statuses, human review, or retry logic for PDF/JPG cookbook scans. Follow root `AGENTS.md`; for persistence or retry changes also use `import-safety`.

## Core distinction: facts vs proposed completions

A scan may be perfectly readable **and still omit critical content**. Do **not** treat every `INCOMPLETE` as an OCR/model failure.

1. **Authoritative transcription** (`observed_recipe`): only information actually supported by the source; preserve ingredient quantities/units, order, steps, temperatures, and times. Do not hallucinate, silently repair, scale, or rewrite.
2. **Diagnostic findings** (`missing_information`): classify issues as `missing_from_source`, `unreadable`, `cropped`, `ambiguous`, or `extraction_failure`, with `severity` (`critical` or `optional`), affected field, and evidence or source location when available.
3. **Unverified suggestions** (`suggestions`): Gemini **may draft a plausible missing quantity, step, cooking time, temperature, or other completion** for the user's later review. These proposals are *inferences, not transcriptions*. Keep them entirely separate from the authoritative recipe data. A suggestion may be marked low confidence or `no_safe_suggestion` when speculation would be misleading.

**Never make a suggested value into recipe content without a recorded human approval or human edit.** An apparently confident model output is not approval. Even when a proposed completion looks obvious, default to review. Do not present suggestions as guaranteed food-safe (e.g., doneness/internal temperatures for meat).

## Suggested output contract (adapt to existing Pydantic models)

Prefer explicit, backward-compatible fields; never overwrite legacy cached `recipes[]` data. Sample conceptual structure:

```json
{
  "name": "Apple Pie",
  "ingredients": ["6 apples", "sugar, amount not shown"],
  "instructions": ["Bake until golden."],
  "missing_information": [
    {
      "field_path": "ingredients[1]",
      "kind": "missing_from_source",
      "severity": "critical",
      "detail": "Sugar quantity is not listed in the scan",
      "source_evidence": "The source says 'sugar' with no amount"
    }
  ],
  "suggestions": [
    {
      "field_path": "ingredients[1]",
      "proposed_value": "1/2 cup sugar",
      "reasoning_summary": "Common plausible amount for a small apple pie; NOT visible in source",
      "confidence": "low",
      "review_status": "pending"
    }
  ]
}
```

This is an **illustrative schema**, not a claim about the current Python code. Keep `source_evidence` short and distinguish *not printed* from *not legible*. If source lacks a title, propose a provisional title separately and flag it. For partially missing directions, suggest specific additions or a complete draft **only in suggestions**, not in `instructions`.

## Implementation procedure

1. Inspect current Pydantic models, Gemini prompt, cache JSON, statuses, and Mealie payload assembly before editing.
2. Add an explicit instruction for Gemini to (a) transcribe visible content, (b) diagnose genuinely missing content, and (c) optionally suggest a plausible completion in a **separate structured field**.
3. Validate suggestion records: nonempty field path and proposal; issue linkage; `pending` by default; no automatic approval; allow multiple candidates when needed without implying one is correct.
4. Define review outcomes: `pending`, `approved`, `edited`, `rejected`; store reviewer choice, final value, and a record of what was proposed. Future importer may apply **only** approved/edited values, never pending/rejected ones. Maintain a traceable review history.
5. Export machine-readable review data plus `needs_review.csv` (source file, recipe identity, field, issue type, missing source evidence, proposed completion, confidence, approval status). Do **not** use CSV alone as the authoritative approval store if repeated edits can lose history.
6. Distinguish `INCOMPLETE` / `NEEDS_REVIEW` from `EXTRACTION_FAILED`; optional yield/metadata omissions alone should not block a recipe. A complete source-derived recipe may import normally; suggested additions never auto-import.
7. For multi-recipe images/PDFs, keep diagnostics and suggestions associated with each **individual recipe**, not only with the source file.
8. If quality escalation re-reads a source, save the new attempt separately. Do not replace the original source-derived transcription with a model's guessed content.

## Tests / acceptance checks

- Legible ingredient list without quantities -> `missing_from_source` + explicit **unverified** proposal, no silent change to ingredients.
- Legible recipe lacking instructions -> potential draft in `suggestions`, with original instructions still empty, not auto-importable.
- Illegible text -> `unreadable` diagnosis, no false assertion that content was omitted by the author.
- Missing yield alone -> optional field, not a critical failure.
- Multi-recipe scan -> each suggestion attached to the correct recipe identity.
- Approval/edit/rejection -> only recorded approved/edited values become eligible for a *separate, explicit* import/update action; all pending suggestions stay out of Mealie.
- Legacy cache JSON loads cleanly with empty diagnostics/suggestions.

Use focused mocked tests. Never require live API calls for routine regression tests.
