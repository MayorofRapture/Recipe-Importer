# Ingredient Extraction Quality and Repair Plan

**Goal:** Flag mixed ingredient/instruction content, support explicit repair through saved review revisions, and audit the current effective recipe library without changing import authority.

**Architecture:** Add one pure semantic validator over `RecipeCandidate`, project findings in catalog/UI, and reuse the existing append-only `ReviewStore` revision path for accepted edits. Keep the historical audit read-only and cache only against extraction and review file metadata.

**Tech Stack:** Python 3.11+, Pydantic, Streamlit, existing unittest suite.

**Spec:** User-provided `Autonomous Ingredient Extraction Quality & Repair` brief.

## Global Constraints

- Never edit source scans, extraction caches, append-only histories, or imported Mealie recipes.
- Repairs remain suggestions until explicit human application; preserve candidate identity and revision conflict checks.
- No Gemini/Mealie network calls in development tests.
- Preserve unrelated `.vscode/` work.
- Commit and push only the verified feature branch; do not merge PR #3.

## Tasks

1. Trace pipeline and reproduce the mixed-content signal with pure validator tests, including legitimate verb-bearing ingredients and substitution notes.
2. Add source-referenced semantic diagnostics; connect extraction quality escalation and Review catalog warnings without changing candidate content or historical approvals.
3. Add a batch repair proposal/editor in Review. Apply as one new revision through existing save/draft and target-binding checks; make protected or unresolved-binding cases read-only.
4. Add a read-only extraction-quality audit using effective current revisions, filters, and invalidation tied to cache/review metadata.
5. Add offline UI/CLI regression tests, README/development notes, browser screenshots and performance measurements where local tooling permits.
6. Run the complete offline suite, syntax/CLI checks, review the diff, commit, and push the development branch.

## Review Focus

- Legitimate ingredient descriptions containing verbs must not be flagged as instructions.
- Repair splitting must not remap old row identities or unresolved proposals by index.
- Existing approvals and imported/uncertain recipes must remain protected.
- Historical audit must not mistake source extraction for the current saved revision.
- Audit caching must invalidate after extraction/review changes and stay off ordinary editor reruns.
