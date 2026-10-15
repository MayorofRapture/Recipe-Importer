# Recipe-Importer UX Review Checklist

Use this checklist against the running Streamlit app and a screenshot before a UI milestone is considered ready.

## Task flow

- [ ] A first-time operator can understand the next action from the empty state.
- [ ] Upload accepts JPG, PNG, WEBP, and PDF by drag-and-drop and file picker.
- [ ] Single-file and batch uploads show previews, progress, cookbook collection/tag choices, and duplicate indications.
- [ ] Progress distinguishes files and recipes, including mixed outcomes inside a multi-recipe source.
- [ ] Review keeps the source visible beside the editor and supports multi-page/multi-recipe navigation.
- [ ] Save draft, approve, reject/skip, and import are separate visible actions.
- [ ] A retry targets only unresolved work and does not erase approvals, corrections, or successful imports.
- [ ] Import creation, tag application, definite failure, and uncertain response are separately understandable.

## Trust and content

- [ ] Source transcription is visually distinct from unverified proposals.
- [ ] Pending proposals cannot appear in the import selection or payload.
- [ ] Missing-from-source, unreadable, ambiguous, and visible-but-not-extracted diagnoses have plain-language explanations.
- [ ] Critical details block approval when required; optional metadata does not.
- [ ] The interface never claims an uncertain Mealie response is a definite failure or success.
- [ ] Technical details are available on demand without crowding the main workflow.

## Efficiency and resilience

- [ ] Keyboard focus order follows the task and remains visible.
- [ ] Repetitive review can proceed without unnecessary page changes or modal hunting.
- [ ] Autosave or an equivalent draft-preservation cue is visible and recoverable after navigation/refresh.
- [ ] Loading, empty, error, partial-success, and success states say what happened and what to do next.
- [ ] The list remains usable with hundreds or thousands of recipes; avoid rendering every full editor at once.
- [ ] Screenshot review checks hierarchy, spacing, readable text, field grouping, and visual noise.

## Representative scenarios

1. **Fried Rice:** upload one readable image, extract, edit one field, confirm the draft save indicator, approve, and verify the recipe becomes importable only after approval.
2. **Caramel Apples:** show a legible source with a genuinely missing quantity. Confirm the missing-source diagnosis and a visibly unverified pending proposal; confirm it cannot enter the import selection until explicitly approved or edited.
3. **Cranberry Date Bars:** exercise an ambiguous or unreadable critical detail, reject or skip it, leave and return to the batch, and confirm the correction and disposition remain intact.
4. **Multi-page PDF:** navigate pages and multiple recipes, import one approved recipe while another needs attention, retry only the unresolved recipe, and confirm the successful recipe is not recreated.
5. **Recovery:** simulate quota exhaustion, OCR fallback, definite Mealie failure, and uncertain Mealie response; verify each has a plain-language next action and no blind duplicate retry.

## Evidence to retain

- [ ] Screenshot(s) of empty, review, needs-attention, and completion states.
- [ ] Playwright steps or trace covering one happy path and one recovery path.
- [ ] Accessibility findings with the input scope stated clearly; mark keyboard/DOM claims unverified when only a screenshot was reviewed.
- [ ] A short list of design decisions and how each reduces effort or prevents a mistake.
