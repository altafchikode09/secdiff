# Phase 1 — 100-case testing log (owner's cycle rule)

Rule (owner, 2026-10-18): run 100 cases → fix small bugs → run again →
fix big bugs → run again → fix ALL remaining → go live only at 100/100.
Suite: `tests/test_100_cases.py` — exactly 100 cases, offline and
deterministic (EDGAR stubbed at the client boundary), plus live-EDGAR
spot checks per cycle for the things stubs cannot prove.

## Case breakdown (100)
- A Resolution & filer sizes — C001–C012 (12): mega-caps, mid/small,
  tiny filer with 1 filing, BRK.B/BRK-B, names (Alphabet, Berkshire
  Hathaway), ambiguous name, unknown ticker, unknown name, lowercase,
  XOM-style wrong-entity trap
- B Forms coverage — C013–C024 (12): DEF 14A / 8-K listing & selection,
  count caps, shortfall errors, 8-K decimal items + canonical titles,
  DEF 14A proposal titles, form notes in reports, 10-K split regression
- C XBRL edge cases — C025–C040 (16): trillions, negatives, zero old,
  zero new, per-share decimals, pure ratios, shares, one-sided concepts,
  absent concepts, retags never merged, alias priority, ambiguous
  values refused, quarterly-only facts, unit preference, fmt boundaries
- D Compare modes — C041–C052 (12): latest-vs-prior, --years 2,
  vs/base year pairs, base-only, vs-only, same-year mistake, reversed
  years, unknown years (available years listed), oldest filing,
  DEF 14A pair, same-year 8-K default pair
- E Report options & exports — C053–C065 (13): risk-only,
  deletions-only, --top N, section by name/item, unknown section,
  CSV/XLS content exactly equal to the on-page table, empty-table
  exports, CLI --export-csv, summary line, source links
- F Web behaviour — C066–C092 (27): home form (all forms + year
  inputs), /demo 404, unknown path 404, malformed inputs (ticker, form,
  top, years) → 400, years reaching the pipeline, proxy page notes +
  no download buttons, year-pinned download links, same-year link
  fallback, CSV/XLS routes, downloads counting toward the free limit,
  unknown-ticker download consuming no slot, 5th company → 402,
  re-view free, account-keyed usage, /stats 404 (anon / non-owner /
  env unset) + 200 owner, POST to download route → 405, GET /report
- G Accounts & stores — C093–C100 (8): signup/login round trip,
  duplicate username, generic 401, corrupt usage file, day rollover,
  unique-visitor-per-day stats, EDGAR-down → 502, name/form parsing

## Bugs found & fixed
| # | Severity | Found | Bug | Fix |
|---|----------|-------|-----|-----|
| B-01 | small (build break) | pre-cycle, impl | cli.py: SUPPORTED_FORMS placed before `from __future__` → SyntaxError | moved constant below imports |
| B-02 | small | cycle 1 (design) | DEF 14A split used 10-K titles: proxy "Item 1" was labelled "Business" | DEF 14A now uses no canned titles (heading text wins); covered by C020 |
| B-03 | big | cycle 2 (live probe) | Real AAPL proxy pair reported "0 deletions, 0 modifications, 0 additions": real proxy headings are split across lines by inline-XBRL layout, so no sections were detected and the diff silently came out empty | whole-document fallback in build_diffs: one clearly labelled FULL section, confidence low; covered by new tests in test_diffing.py; live AAPL proxy now 457 del / 353 mod / 482 add |
| B-04 | small | cycle 2 (follow-up of B-03) | With the fallback, the 60-hunk per-section cap (deletions first) showed ONLY deletions and the summary totals hid all modifications/additions | FULL fallback uses max_hunks=2000 so totals reflect the document |
| B-05 | small | cycle 1 (design) | Download links on a same-year pair (routine for 8-Ks) pinned vs_year==base_year, a combination the compare itself rejects — links would have 400'd | years pinned only when the two filing years differ; covered by C079 |

## Cycles
- Cycle 1: 100-case suite → **100/100 passed** (B-02, B-05 fixed at
  design time before the run; B-01 fixed during implementation).
  Existing suite: 89/89.
- Cycle 2: live-EDGAR probing of the new forms found B-03 (big) and
  B-04; both fixed. Full suite after fixes: **191/191** (89 existing +
  100 cases + 2 new fallback tests). Live checks: AAPL DEF 14A
  457/353/482 with proxy note + no numbers table; AAPL 8-K sections
  2.02/9.01 with real changes + event-filing note; AAPL 10-K
  2025-vs-2023 two-year compare 199/129/68, 75 XBRL; CSV export equals
  the HTML table (Revenue $383.29B → $416.16B in both).
- Cycle 3: full re-run — **100/100 in the owner's suite, 191/191
  overall**. Regression anchors re-verified live after all fixes:
  AAPL 10-K default = 155 del / 162 mod / 50 add / 75 XBRL (exact);
  "Alphabet" → GOOG, 272 deletions; BRK.B → BRK-B, 197 deletions.
  Local web smoke (real server, cached data): home shows 8-K + DEF 14A
  + year inputs; AAPL report shows anchors + pinned download links;
  /report.csv returns the table; proxy page shows true totals.

Final state: **100/100 — clear for the deploy step** (deploy + live
verification on secdiff.onrender.com happen after this log, per the
owner's ordering: evidence first, then go live).
