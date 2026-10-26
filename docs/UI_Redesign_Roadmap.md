# UI redesign implementation roadmap

**Status: UX-0 verification baseline completed. The Review layout redesign is not started and remains outside this milestone.** The current UI is documented in [the assessment](UI_UX_Audit.md); the behavior contract is in [the product experience](UI_Product_Experience.md).

## Guardrails for every phase

- Work on `feature/complete-recipe-workspace`; do not merge PR #3.
- Preserve `ReviewStore`, `DraftStore`, candidate identity, source hashes, effective-review projection, append-only import/reconciliation ledgers, and existing CLI behavior. Do not change approval authority or Mealie payload/API behavior as part of presentation work.
- No live Gemini or Mealie calls in ordinary UI tests. Use sanitized synthetic sources and an isolated output directory.
- Keep cookbook range import disabled.
- Do not use local paths, full source hashes, recipe text, or credentials in user-facing screenshots or logs. Keep `.vscode/`, `graphify-out/`, caches, histories, and source scans untouched.
- Prefer native Streamlit controls. A custom component requires a small documented proof that native controls cannot meet a named interaction/accessibility requirement.
- Before any phase that changes presentation, use Playwright or the repository's local CDP runner on a real rendered browser. AppTest alone cannot establish focus, accessibility tree, reflow, contrast, or clipping.
- Stop for user inspection at the phase checkpoints below. No live rollout is implied by a completed local milestone.

## Prioritized work

The implementation sequence is intentionally split into five gates:

1. **Browser/accessibility verification (UX-0):** completed. The baseline, harness corrections, focus regression, and sanitized evidence are recorded in [the audit](UI_UX_Audit.md).
2. **Review layout (D1):** next, but not started here. Keep backend review authority and candidate identity unchanged.
3. **Contextual issues and editing efficiency (D2):** follow the approved layout; keep proposals separate and visibly unverified.
4. **Upload and Import/History refinements (D3):** preserve job lifecycle, import eligibility, tag outcomes, and reconciliation behavior.
5. **Performance and final usability validation (D4):** validate representative scale and recovery with synthetic data, then record evidence limits.


| Phase | Priority / effort | Scope and user value | Current behavior | Acceptance evidence | Regression risks |
| --- | --- | --- | --- | --- | --- |
| **UX-0 - Browser/accessibility verification** | Completed / Small | Resolve harness assumptions; inspect rendered DOM and accessibility tree; record responsive, keyboard, focus, and action visibility without redesigning Review. | The title locator resolves through the accessible Recipe title label. At 320px, Expand sidebar exposes navigation and keyboard reaches Review. | Synthetic Chromium journeys at 1920x1080, 1440x900, 768x900, and 320x900; AX names for title/save/approve; focus restoration after save/candidate navigation; sanitized screenshots. | Full screen-reader behavior and keyboard-only completion remain unverified. |
| **D1 — Review viewport and focus layout** | Highest impact / Medium | Reduce navigation stack height, keep source/editor readable at wide desktop, use labeled Source/Recipe switching at narrow widths, and keep primary review actions close to recipe identity. Improves repetition speed and source comparison. | Current side-by-side split is useful at 1440/1920, cramped at 768, and stacked/long at 320. Save/Approve fall below the initial 1440×900 view. | Chromium at 1440×900, 1920×1080, 768×900, 320×900 and 200% text; no horizontal overflow; actual source text and editor are readable; save/approve reachable without losing changes; keyboard order follows visual order; focus returns to the control that opened a pane/queue. | Streamlit reruns and widget state can lose committed or focused text when layout mode changes. Do not move a focused widget or recreate it with a conflicting key. |
| **D2 — Local issues and proposals in editing context** | High / Medium | Place a concise issue/proposal summary beside its field/step, with details expandable. Decreases scrolling and makes uncertainty actionable. | Issues/proposals are not always colocated with the row they concern; source note detail can interrupt the editing scan. | Synthetic cases cover missing, unreadable, ambiguous and visible-but-not-extracted details; pending proposal remains visually unverified; each decision is attributable; critical blocker stays distinct from optional metadata. | Accidental visual merge or status language could make unverified content appear authoritative/importable. Keep all transitions in existing review services. |
| **D3 — Upload and Import/History refinements** | Medium / Medium | Put the selected/recent batch, per-file results and active job before batch creation; distinguish import-ready approved revisions from history and reconciliation. Improves resumption and safer recovery. | Upload setup and range operations form a tall page. Import preflight and searchable history share one long page but keep separate semantics. | AppTest for batch selection, partial upload, validation, pause/resume and range exclusion; browser at desktop and 320px; mocked create/tag success, partial failure and uncertain create; protected history remains non-selectable. | Risk of hiding cloud consent, confusing ordinary scans with cookbook ranges, or surfacing protected history as importable. Reuse existing job and import services. |
| **D4 — Performance and final usability validation** | Release gate / Medium | Validate repeated use, interrupted work and larger libraries; assess practicality for one user managing hundreds of recipes. | Local timings exist for specific synthetic review operations, but no user study or released design validation establishes efficiency. | Walkthrough against the proposed goals in the product spec; 100+ synthetic candidates and history; keyboard-only happy/recovery paths; screenshots and recorded issues; no production data. | Synthetic performance and one-person walkthroughs do not establish broader usability or service readiness. Record evidence limits. |

## Quick wins versus architectural changes

**Quick wins:** replace the default local-path caption with short status and optional diagnostics; tighten the vertical stack above Review panes; put saved/approved revision state near the title; preserve the verified focus handoff; keep Upload and Import/History refinements in their dedicated phase.

**Layout work:** breakpoint/reflow policy for Review, source/editor switcher, queue access at narrow widths, and scroll/focus restoration.

**Potential architecture only if a blocker is demonstrated:** a small state-preserving interaction adapter for source/editor switching or focus restoration. Do not preemptively introduce new persistence, review, import or navigation frameworks.

## Verification and stop points

For each approved phase, create or update focused tests before behavior changes, run them with the existing unittest/AppTest suite, inspect the rendered browser state, and record exact commands/counts. No phase authorizes rewriting old histories or invoking real services. Stop before D1 in this UX-0 delivery; later milestone work requires its own explicit scope.

Release readiness remains a separate track: live Gemini model/quota behavior, compatibility with the installed Mealie version, and controlled range-candidate import enablement. This redesign must not enable range import.

## Expected effort

- UX-0: completed
- D1: 2–4 days
- D2: 1–3 days
- D3: 2-3 days
- D4: 1-2 days plus user walkthrough availability

These are planning estimates for one experienced Streamlit engineer, contingent on current tests and one milestone at a time. Live integration work is excluded.
