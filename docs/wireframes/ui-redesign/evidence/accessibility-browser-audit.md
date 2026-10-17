# Browser accessibility review

Scope: the Review workspace and Upload error state in the local fixture application. Reviewed 2026-10-16 in cached Chromium through the repository's dependency-free CDP helper. Browser sandbox stayed enabled; requests were restricted to the local fixture server at `127.0.0.1:8777`, with fixture mode, synthetic PDF/PNG inputs, and isolated output. No Gemini or Mealie service was contacted.

Evidence: [Review workspace at 1440x900](browser-review-workspace-1440x900.png), [Review at 320x900 with skip-link focus](browser-review-focus-320x900.png), [Review with 200% root text size](browser-review-text-200.png), [200% instruction detail](browser-review-text-200-instructions.png), [320px instruction detail](browser-review-320-instructions.png), and [Upload partial-success error](browser-upload-rejected-file-error-1440x900.png). Before each Review screenshot, the CDP helper required zero skeleton placeholders, a complete source-page image, a nonempty title, and nonempty instruction content for three consecutive checks. Each settled capture showed a 900px-wide source image and two populated instruction fields. The fresh desktop capture was taken at `window.scrollY=0`, document/body/main scroll positions `0`, and main block top `0`; navigation link boxes are within y=16..44. At 1440x900, the panes begin at y=239, source recipe text at about y=380, Ingredients at y=415, and Instructions at y=682. At 320x900 the page becomes single-column with no horizontal overflow. At 200% root text (computed 32px), there is no horizontal overflow; the workspace extends vertically and requires scrolling.

## Finding

- **P1 - 1.1.1 Non-text content:** the source preview image has alternative text `Recipe source page 1`. This identifies the page but does not convey the printed recipe content that an operator compares against the extraction. Provide an accessible source transcription or equivalent text alternative while retaining source provenance and keeping uncertain OCR clearly distinguished from authoritative content.

## WCAG 2.2 criteria ledger

### Checked in the captured state

- **1.4.4 Resize text:** the 200% root-font render remains readable and does not clip horizontally; content wraps and continues below the viewport. This was a CSS root-font-size simulation, not an operating-system magnifier test.
- **1.4.10 Reflow:** the 320px viewport is single-column with no horizontal document overflow.
- **2.4.4 Link purpose (in context):** visible navigation links are named Upload, Review, and Import & history.
- **2.4.6 Headings and labels:** Review is an H1. Visible field labels include Recipe title, Page, Source view, Ingredient 1/2, and Step 1/2.
- **2.5.8 Target size (minimum):** measured visible navigation and action controls are at least 24 CSS px in each dimension; primary controls are 40px high. The displayed source fullscreen control is 24x24.
- **3.2.2 On input:** in the exercised upload, selection, page and review flow, changing a value did not unexpectedly navigate or change context.
- **3.3.1 Error identification:** the rejected PDF message names the file and explains rejection in text while separately showing the accepted PNG.
- **3.3.2 Labels or instructions:** visible labels identify the recipe title, page, source view, ingredient and instruction controls.

### Not verifiable from this review

- **1.3.1 Info and relationships:** the page title is an H1, but the full accessibility tree and relationships between all rendered sections and controls were not inspected.
- **1.4.3 Contrast minimum:** theme colors give 13.33:1 for `#202C2B` on `#F7F6F2` and 6.98:1 for white text on the `#17645B` primary button. These representative pairs do not cover every alert, muted, disabled, and control state, so a full criterion result is unverified.
- **1.4.11 Non-text contrast:** borders, icons and all control-state indicators were not measured exhaustively.
- **2.1.1 Keyboard:** the first Tab reaches the visible skip link, but the complete workflow was not tested keyboard-only.
- **2.1.2 No keyboard trap:** no full keyboard traversal was performed.
- **2.4.7 Focus visible:** the skip link has a visible teal focus ring in the 320px capture; focus visibility on every interactive element was not verified.
- **2.4.11 Focus not obscured:** the focused skip link is fully visible in the captured state; other controls and scroll positions were not exhaustively tested.
- **3.3.8 Accessible authentication:** no authentication flow is presented by this local fixture screen, so this criterion was not exercised.
- **4.1.2 Name, role, value:** the DOM snapshot includes visible labels and native controls, but no complete browser accessibility tree was captured.

This is a scoped expert review, not an axe/Lighthouse scan, screen-reader test, user study, or WCAG conformance certification. The UI flow checks establish only the captured states and interactions.
