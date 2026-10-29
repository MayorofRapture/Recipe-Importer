# Manual approval and recipes without instructions

The Review workspace supports source-verified approval and an explicit manual
path for historical recipes whose original scan is unavailable.

## Restore or manually review a missing source

In Review, open **Source** for the selected recipe. The page shows the original
filename and last known path when available.

Use **Locate the original PDF or image** to restore a scan. The app hashes the
uploaded bytes before saving them under `output/recovered_sources/`. Only an
exact match to the recipe's existing source hash is linked. A different file is
not saved or linked, and the source hash is never replaced.

If the original bytes are permanently unavailable, review the current title,
ingredients, instructions, notes, uncertainties, and findings. Check the
acknowledgment and add a review note, then choose **Approve manually & next**.
The append-only approval event records `manual_source_unavailable`, the exact
candidate, source hash, extraction, review revision, content digest, reviewer
acknowledgment, rationale, and timestamp. It does not claim source verification
or resolve source findings. Identity, unresolved suggestions, critical finding
dispositions, stale revisions, and protected Mealie outcomes continue to block
approval.

## Recipes with no preparation instructions

Missing instructions remain incomplete unless a reviewer explicitly confirms
that none are needed. Choose **Confirm that no instructions are needed** and
select a reason (or explain another reason), then record the decision. The
decision is bound to the exact saved review revision and content digest. Any
later recipe edit requires a new decision and approval.

Other choices preserve the existing review workflow: add source-supported
instructions in the editor, request inferred suggestions using the existing
Gemini consent and routing workflow, or keep the recipe as an incomplete draft.
AI suggestions remain separate from recipe data until explicitly reviewed.

For a confirmed no-instructions recipe, the Mealie JSON-LD payload contains
`"recipeInstructions": []`; it never inserts a made-up preparation step.
The offline client tests verify the generated payload reaches the mocked
Mealie boundary unchanged. Acceptance of an empty instruction list by the
installed Mealie v3 endpoint has not been verified against a live server. The
current upstream `mealie-next` recipe creation code fills an empty instruction
list with a localized default step ([source](https://github.com/mealie-recipes/mealie/blob/mealie-next/mealie/services/recipe/recipe_service.py)).
That is not version-matched evidence for the installed server, so the
import preflight blocks such recipes by default. After verifying that the
installed endpoint preserves an empty list, set
`MEALIE_SUPPORTS_EMPTY_INSTRUCTIONS=true` in the local environment and restart
the app. The payload remains empty. If Mealie rejects it, the failure is
recorded against that exact revision and the importer will not post it again;
edit the recipe or verify endpoint support before another create attempt.

Import preflight and import history display the approval method, review note,
and no-instructions reason. GUI and modern CLI import paths use the exact
effective review authority and preserve the original source hash.
