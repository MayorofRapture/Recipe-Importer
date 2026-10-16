# Uploads, extraction and recovery

Synthetic example batch: **Weeknight cookbook**, optional Mealie tag **Weeknight scans**. Local batch grouping is not Mealie cookbook membership. All designs are proposed.

## U1 — First-time intake / global navigation

```text
Recipe Importer        Uploads [active]       Review       Import & history       Help
-----------------------------------------------------------------------------------
Add recipes
Turn scans into recipes you can check before sending to Mealie.

Batch name [Weeknight cookbook              ]   Mealie tag (optional) [Weeknight scans]
                                               Used for filtering in Mealie.
┌─────────────────────────────────────────────────────────────────────────────────┐
│ Drop scans or a cookbook PDF here                         [Choose files]         │
│ JPG, JPEG, PNG, WEBP, PDF · file/page limits shown from actual configuration      │
└─────────────────────────────────────────────────────────────────────────────────┘
Files                         Pages          Result                    Action
[preview] fried-rice.jpg         1            Added                     Remove from batch
[preview] cookbook.pdf          12            Added                     Preview pages
[preview] rice-copy.jpg          1            Previously seen           Open existing work
          notes.txt              —            Unsupported type          Choose another file

Processing [Local OCR] [Gemini extraction] [OCR + assisted structuring]
Gemini extraction sends these images/PDFs to Google. Review happens locally.
[Start extraction: 2 new files]      Advanced options ▾
```

Choose files/drop stages only accepted files after successful durable write. Duplicates do not inflate new-file count. Remove from batch removes membership, not original source/cache/history; no permanent-delete control. Before R4, existing pages remain honest placeholders, not this form pretending to work.

Advanced options show configured routing/limits and capability validation. Local OCR copy explicitly says no cloud; assisted structuring says numbered OCR text may be sent. Missing Tesseract offers configure/help or eligible other mode. Save batch name/tag on commit; changing tag never alters imported history. Interrupted/partial staging retains accepted sources and points to failed files.

## U2 — Active extraction

```text
Recipe Importer        Uploads [active]       Review       Import & history
Batch [Weeknight cookbook ▾]                                  [Add files]
-----------------------------------------------------------------------------------
Processing scans                              [Pause after current file]
18 of 24 files processed      [======================--------]
31 recipes available · 6 need attention        [Review available recipes]

Source                Processing result                        Next action
cookbook.pdf          8 recipes found · 2 need review            Review recipes
fried-rice.jpg        Ready to review                           Review
bars.jpg              Text saved; needs structuring             Open source
apples.jpg            Processing                                Details
page-19.jpg           Waiting                                   —
-----------------------------------------------------------------------------------
Details ▾  Attempts, OCR method, model route, local counters, event log
```

Progress percentage uses known file count, not guessed recipe denominator. Completed sources can have mixed recipe states; clicking Review retains job in backend. Polling never starts jobs. Pause prevents scheduling after in-flight file(s) finish; worker count means more than one file may already be running, so show that fact rather than promising instant pause. Existing candidates/edits remain usable if network stops.

## U3 — Paused / failure / completed states

```text
Processing paused — request allowance reached
18 of 24 files processed. Your recipes and corrections are saved.
[Review 31 available recipes]   [Resume when quota is available]   Details ▾
Reset time not available. Other applications may share this provider allowance.
```

Resume revalidates provider eligibility and unresolved scope; it does not blindly rotate models. Disabled resume explains prerequisite. A transient source error has **Try failed files again**, source path/title and safe next step; not-created candidates remain in source activity rather than disappearing from inventory.

```text
Source needs attention — this scan could not be read
No recipe was produced from page-19.jpg. Earlier recipes are unchanged.
[Preview source]   [Try local OCR]   [Try extraction again]   Details ▾
```

Only offer supported recovery modes; cloud action repeats disclosure and preserves attempt lineage. “No recipe found” is reserved for a successful extraction that genuinely produced none.

```text
All 24 files processed
38 recipes found · 32 to review · 6 need attention
[Continue review]    See source results
```

Completion means processing completed, not recipes approved/imported. Empty batch says Add files; filter-empty says Clear filters. A storage error preserves current accepted files and gives Retry save, not a fresh batch that duplicates staging.

## U4 — Intentional re-extraction / identity compare (R5)

```text
Try a new extraction — cookbook.pdf
Targets: Cranberry & Caramel Date Bars
Protected siblings: Fried Rice (Imported), Lemon Cake (Ready to import)
The whole PDF may be processed; protected recipe outcomes will be preserved.
Earlier extraction, draft and decisions remain available.
[Start new attempt] [Cancel]

New results need matching
Existing candidate                  New candidate                    Choice
Cranberry & Caramel Date Bars        Date Bars                         [Same recipe ▾]
Imported Fried Rice                 Fried Rice                        Protected / unchanged
Unmatched                           Caramel Topping                    [Defer ▾]
[Save mapping] [Cancel]
```

No array-position matching. Same title is not conclusive across ambiguous candidates. Mapping does not import/approve. Changed source bytes need a lineage relationship established explicitly; no automatic deduplication by recipe title. Save failure retains the comparison choices. Cancel leaves existing draft authoritative for continued review.

## Keyboard / responsive requirements

File picker is equivalent to drag/drop. Status updates announce whole contextual phrases without stealing focus. Batch/tag labels remain visible; file results use text. At narrow widths each file is a labeled stacked row and controls wrap; a single-column page scroll replaces a dense desktop table. No horizontal scroll for filenames/results; full name is discoverable to keyboard users. Check screenshot and DOM/keyboard evidence at R4/R5, not just AppTest uploader existence.
