# Phase 3 — 100-case log (owner's rule)

Phase 3 adds, on top of the Phase 1–2 pipeline: (1) search-in-report
(`?q=` — only the hunks mentioning a word/phrase, term marked, honest
count, applied after the other filters); (2) a summary PDF download
(`/report.pdf`, hand-rolled stdlib PDF: header, counts, score, top
deletions — labelled a summary, not a page copy); (3) report share
links (canonical URL on every report page, reproducing the exact
filing pair + options); (4) a share card (`/card` — 1200×630 PNG,
stdlib encoder + built-in 5×7 font: counts, score, biggest deletion,
report URL). All four follow the existing accounting: resolve first,
same 4-companies/day limit by CIK, downloads of an already-viewed
company free, `/demo` 404, `/stats` owner-only.

Suite: `tests/test_phase3_cases.py` — exactly 100 cases, S001–S100.
A  Search core (matching, phrases, counts, marking, composition)  S001–S022
B  Search web + URL safety (GET/POST, escaping, 400s, slots)      S023–S036
C  PDF (structure/xref, content, pagination, routes, limits)     S037–S060
D  Share links (contents, pins, reproduction equality)           S061–S074
E  Share card (PNG validity, data, pixels, routes, limits)       S075–S100

## Bugs found & fixed
| # | Severity | Found | Bug | Fix |
|---|----------|-------|-----|-----|
| B-01 | small (build break) | implementation | The file-writing path mangled six `cookie=` + string-literal values in the new test file into `<redacted>`, eating a closing paren (SyntaxError) | Repaired the six lines to pass the visitor cookie via a `_VID`/`vid` variable (the Phase 1 suite's pattern); file parses, suite runs |
| B-02 | small | cycle 1 (S012) | Test asserted a literal-match property incorrectly (the non-matching string "axb" is present in the source text itself) | Assertion now checks the marked span is exactly the literal term and the lookalike is unmarked |
| B-03 | small | cycle 1 (S041) | Test forgot PDF string escaping when checking the score line | Expected string now escapes ( ) the way the PDF writer does |
| B-04 | small | cycle 1 (S061) | Test asserted the share URL ended at `form=10-K`, but the canonical URL correctly continues with the pinned years | Assertion checks the prefix up to the next `&amp;` |

No product-code bugs were found in cycles 1–3: the three cycle-1
failures were all test-side, and the live cycle confirmed the
features behave as specified on real data.

## Cycles
- Cycle 1: Phase 3 suite → **97/100** (B-02, B-03, B-04 — all test
  assertions; B-01 fixed before the run). Existing suite unaffected.
- Cycle 2: fixes applied, re-run → **100/100**; full suite
  **391/391** (291 pre-existing + 100 new).
- Cycle 3: live-EDGAR probing of the new surfaces:
  - AAPL 10-K anchors intact: 155 del / 162 mod / 50 add / 75 XBRL,
    score 80/100.
  - Search "environmental, social and governance" → **3 deletions +
    1 modification**, including the two known ESG-subsection
    deletions ("Many governments, regulators, investors…
    increasingly focused on environmental, social and governance
    considerations" and "Responding to these environmental, social
    and governance considerations…"), term marked, remove-filter
    link present.
  - Search "ESG" → honest zero-hit state ("0 changes mention",
    "No changes in this report mention 'ESG'"). This is CORRECT:
    Apple's filings spell the phrase out and the letters E-S-G never
    occur contiguously in any diff hunk — a substring search must
    not pretend otherwise. (The verification brief expected 'ESG'
    to surface the deletion; the spelled-out phrase is the term
    that does.)
  - AAPL PDF: valid structure, carries "155 deletions - 162
    modifications - 50 additions" and "Change-intensity score:
    80/100". AAPL proxy PDF carries its 457 deletions.
  - AAPL card data: 155/162/50, score 80, 75 XBRL changed; PNG
    renders (the biggest deletion by the diff's own size ranking is
    an Item 8 cash-flow-statement block — a known property of this
    report's table text, honestly the largest deletion hunk).
  - "Alphabet" → GOOG (272 deletions); BRK.B → 197 deletions.
  - AAPL DEF 14A anchors intact: 457 del / 353 mod / 482 add.
- Cycle 3 confirmation re-run: **100/100** in the owner's suite,
  **391/391** overall.

## Design decisions recorded
- Search counts are matching HUNKS ("N changes mention X"), never
  occurrences; a hunk mentioning the term three times is one change.
- The search term is validated once at the form boundary (length
  cap, no control/markup-breaking characters, must contain a letter
  or digit) and is always HTML-escaped on output and urlencoded in
  links; it is deliberately excluded from the canonical share URL.
- The score shown on a searched page follows the project's existing
  convention (computed from the changes the page displays — the same
  as risk-only / top-N views); the banner states the filter is on.
- PDF/card ignore `q`: they summarise the whole (option-filtered)
  report, and their links never carry a visitor's search view.
- Card text uses the built-in 5×7 font (uppercase); characters the
  font cannot draw are transliterated (– — ' ' " " • …) or dropped,
  so card text is always renderable and legible, never garbled.
- /card returns 404 for an unknown ticker/company (the card does not
  exist), while /report.pdf keeps the report family's 400 error page
  for unresolved tickers — both before any free slot is consumed.

Final state: **100/100 — clear for the deploy step** (deploy + live
verification on secdiff.onrender.com happen after this log, per the
owner's ordering: evidence first, then go live).
